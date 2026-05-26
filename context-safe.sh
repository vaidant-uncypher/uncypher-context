#!/usr/bin/env bash
#
# context-safe.sh — reliable entry point for context.py
#
# Why this exists
# ---------------
# Project folders can be mounted into agent sandboxes through virtiofs/FUSE or
# similar filesystems. Those mounts do not always support the POSIX lock/fsync
# behavior SQLite needs for write transactions. Running sqlite3 commits
# directly against context.db from such a mount can cause disk I/O errors or DB
# corruption.
#
# This wrapper makes context.py reliable by:
#   1. taking a single global lock before touching context.db;
#   2. copying context.db to a unique native /tmp working directory;
#   3. running SQLite only against that local copy;
#   4. integrity-checking before publishing;
#   5. preserving both context_backup.db and timestamped snapshots; and
#   6. atomically replacing the canonical DB only after validation passes.
#
# Use this from all agent sessions and, preferably, from the host terminal too.

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CANONICAL_DB="${SCRIPT_DIR}/context.db"
BACKUP_DB="${SCRIPT_DIR}/context_backup.db"
BACKUP_DIR="${SCRIPT_DIR}/context_backups"
CONTEXT_PY="${SCRIPT_DIR}/context.py"
LOCK_DIR="${SCRIPT_DIR}/.context-safe.lock"
ENV_FILE="${SCRIPT_DIR}/.context.env"

LOCK_WAIT_SECONDS=180
LOCK_STALE_SECONDS=1800
KEEP_WORK=0
LOCK_ACQUIRED=0
WORK_DIR=""
HAD_CANONICAL=0

timestamp_utc() {
    date -u +"%Y%m%dT%H%M%SZ"
}

validate_db() {
    local db_path="$1"
    python3 - "${db_path}" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
try:
    conn = sqlite3.connect(path)
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    conn.close()
except Exception as exc:
    print(f"ERROR: {exc}")
    sys.exit(1)

messages = [row[0] for row in rows]
print("; ".join(messages))
sys.exit(0 if messages == ["ok"] else 1)
PY
}

lock_mtime_epoch() {
    stat -f %m "${LOCK_DIR}" 2>/dev/null || stat -c %Y "${LOCK_DIR}" 2>/dev/null || echo 0
}

acquire_lock() {
    local start now age mtime
    start="$(date +%s)"

    while ! mkdir "${LOCK_DIR}" 2>/dev/null; do
        now="$(date +%s)"
        mtime="$(lock_mtime_epoch)"
        age=$((now - mtime))

        if [[ "${age}" -gt "${LOCK_STALE_SECONDS}" ]]; then
            echo "context-safe: removing stale lock (${age}s old): ${LOCK_DIR}" >&2
            rm -rf "${LOCK_DIR}"
            continue
        fi

        if [[ $((now - start)) -gt "${LOCK_WAIT_SECONDS}" ]]; then
            echo "context-safe: timed out waiting for lock: ${LOCK_DIR}" >&2
            exit 4
        fi

        sleep 1
    done

    LOCK_ACQUIRED=1
    {
        echo "pid=$$"
        echo "created_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
        echo "command=$*"
    } > "${LOCK_DIR}/owner"
}

cleanup() {
    if [[ "${KEEP_WORK}" != "1" && -n "${WORK_DIR}" && -d "${WORK_DIR}" ]]; then
        rm -rf "${WORK_DIR}"
    fi
    if [[ "${LOCK_ACQUIRED}" == "1" && -d "${LOCK_DIR}" ]]; then
        rm -rf "${LOCK_DIR}"
    fi
}

trap cleanup EXIT

# --- Preflight ----------------------------------------------------------------
if [[ ! -f "${CONTEXT_PY}" ]]; then
    echo "context-safe: context.py not found at ${CONTEXT_PY}" >&2
    exit 2
fi
if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
fi

acquire_lock "$@"
mkdir -p "${BACKUP_DIR}"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cowork-context.XXXXXX")"
WORK_DB="${WORK_DIR}/context.db"

# --- 1. Sync canonical DB to an isolated native working copy ------------------
if [[ -f "${CANONICAL_DB}" ]]; then
    HAD_CANONICAL=1
    cp "${CANONICAL_DB}" "${WORK_DB}"
    rm -f "${WORK_DB}-journal" "${WORK_DB}-wal" "${WORK_DB}-shm"

    INTEGRITY_RESULT="$(validate_db "${WORK_DB}")" || {
        echo "context-safe: canonical DB failed integrity check: ${INTEGRITY_RESULT}" >&2

        if [[ -f "${BACKUP_DB}" ]]; then
            BACKUP_CHECK="${WORK_DIR}/backup-check.db"
            cp "${BACKUP_DB}" "${BACKUP_CHECK}"
            BACKUP_INTEGRITY="$(validate_db "${BACKUP_CHECK}")" || {
                echo "context-safe: backup DB also failed integrity check: ${BACKUP_INTEGRITY}" >&2
                echo "context-safe: preserving working files at ${WORK_DIR}" >&2
                KEEP_WORK=1
                exit 5
            }

            BAD_SNAPSHOT="${BACKUP_DIR}/malformed-$(timestamp_utc).db"
            cp "${CANONICAL_DB}" "${BAD_SNAPSHOT}" || true
            RESTORE_STAGED="${CANONICAL_DB}.restore.$$"
            cp "${BACKUP_DB}" "${RESTORE_STAGED}"
            mv "${RESTORE_STAGED}" "${CANONICAL_DB}"
            cp "${BACKUP_DB}" "${WORK_DB}"
            echo "context-safe: restored canonical DB from context_backup.db" >&2
            echo "context-safe: malformed DB snapshot: ${BAD_SNAPSHOT}" >&2
        else
            echo "context-safe: no backup DB available; preserving working files at ${WORK_DIR}" >&2
            KEEP_WORK=1
            exit 5
        fi
    }
else
    # First run: context.py will create the working SQLite DB.
    HAD_CANONICAL=0
fi

# --- 2. Run context.py against the local working DB ---------------------------
CONTEXT_PY_EXIT=0
CONTEXT_DB="${WORK_DB}" \
CONTEXT_DISABLE_CONFIDENCE_CACHE=1 \
python3 "${CONTEXT_PY}" "$@" || CONTEXT_PY_EXIT=$?

if [[ ${CONTEXT_PY_EXIT} -ne 0 ]]; then
    echo "context-safe: context.py exited ${CONTEXT_PY_EXIT}; canonical DB left untouched" >&2
    echo "context-safe: local working DB preserved at ${WORK_DB}" >&2
    KEEP_WORK=1
    exit ${CONTEXT_PY_EXIT}
fi

# --- 3. Publish only real DB changes -----------------------------------------
if cmp -s "${CANONICAL_DB}" "${WORK_DB}"; then
    exit 0
fi

INTEGRITY_RESULT="$(validate_db "${WORK_DB}")" || {
    echo "context-safe: refusing to publish malformed working DB" >&2
    echo "context-safe: integrity_check => ${INTEGRITY_RESULT}" >&2
    echo "context-safe: canonical DB left untouched; working DB preserved at ${WORK_DB}" >&2
    KEEP_WORK=1
    exit 3
}

# Preserve the previous canonical DB before publishing a new one.
if [[ "${HAD_CANONICAL}" == "1" && -f "${CANONICAL_DB}" ]]; then
    SNAPSHOT="${BACKUP_DIR}/context-$(timestamp_utc).db"
    cp "${CANONICAL_DB}" "${SNAPSHOT}"

    BACKUP_STAGED="${BACKUP_DB}.staged.$$"
    cp "${CANONICAL_DB}" "${BACKUP_STAGED}"
    mv "${BACKUP_STAGED}" "${BACKUP_DB}"
fi

# Keep the snapshot directory bounded.
ls -1t "${BACKUP_DIR}"/context-*.db 2>/dev/null | tail -n +31 | while read -r old_snapshot; do
    rm -f "${old_snapshot}"
done || true

STAGED="${CANONICAL_DB}.staged.$$"
cp "${WORK_DB}" "${STAGED}"

STAGED_CHECK="${WORK_DIR}/staged-check.db"
cp "${STAGED}" "${STAGED_CHECK}"
STAGED_INTEGRITY="$(validate_db "${STAGED_CHECK}")" || {
    rm -f "${STAGED}"
    echo "context-safe: staged DB failed integrity check: ${STAGED_INTEGRITY}" >&2
    echo "context-safe: canonical DB left untouched; working DB preserved at ${WORK_DB}" >&2
    KEEP_WORK=1
    exit 3
}

mv "${STAGED}" "${CANONICAL_DB}"

POST_CHECK="${WORK_DIR}/post-publish-check.db"
cp "${CANONICAL_DB}" "${POST_CHECK}"
POST_INTEGRITY="$(validate_db "${POST_CHECK}")" || {
    echo "context-safe: published DB failed post-check: ${POST_INTEGRITY}" >&2
    if [[ -f "${BACKUP_DB}" ]]; then
        echo "context-safe: restoring previous canonical DB from ${BACKUP_DB}" >&2
        RESTORE_STAGED="${CANONICAL_DB}.restore.$$"
        cp "${BACKUP_DB}" "${RESTORE_STAGED}"
        mv "${RESTORE_STAGED}" "${CANONICAL_DB}"
    else
        echo "context-safe: no previous backup available; preserving working files at ${WORK_DB}" >&2
    fi
    KEEP_WORK=1
    exit 6
}

exit 0
