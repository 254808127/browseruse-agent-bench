#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
log_path_file="$(mktemp)"

cleanup() {
  rm -f -- "${log_path_file}"
}

capture_output() {
  local line
  local log_path

  while IFS= read -r line || [[ -n "${line}" ]]; do
    printf '%s\n' "${line}"
    if [[ ! -s "${log_path_file}" && "${line}" == *"Logging to file: "* ]]; then
      log_path="${line##*Logging to file: }"
      log_path="${log_path%$'\r'}"
      printf '%s\n' "${log_path}" >"${log_path_file}"
    fi
  done
}

trap cleanup EXIT

cd "${repo_root}"

set +e
env \
  BUBENCH_BROWSER_USE_DIAG=1 \
  BUBENCH_BROWSER_USE_CDP_LIVENESS_DIAG=1 \
  uv run bubench run \
    --agent browser-use \
    --data LexBench-Browser \
    --split All \
    --mode by_id \
    --id 3012 \
    --model-name grok-4.5 \
    --timeout 1200 \
    --concurrency 1 \
  2>&1 | capture_output
run_status="${PIPESTATUS[0]}"
set -e

log_path=""
if [[ -s "${log_path_file}" ]]; then
  IFS= read -r log_path <"${log_path_file}"
fi

if [[ -z "${log_path}" || ! -f "${log_path}" ]]; then
  printf '\nUnable to locate the log emitted by this run.\n' >&2
  if [[ "${run_status}" -eq 0 ]]; then
    exit 1
  fi
  exit "${run_status}"
fi

printf '\n=== CDP liveness analysis: %s ===\n' "${log_path}"
if ! rg -n '\[browser-use cdp-liveness\] (start|sample|summary)|TIMEOUT HERE' "${log_path}"; then
  printf 'No liveness probe or watchdog timeout records found.\n'
fi

if rg -q '\[browser-use cdp-liveness\] start methods=' "${log_path}"; then
  printf '\nProbe trigger status: TRIGGERED\n'
else
  printf '\nProbe trigger status: NOT TRIGGERED\n'
  printf 'Zero request counts mean no probe send was attempted; they do not indicate a failed send.\n'
fi

printf '\n=== Heavy probe CDP request counts ===\n'
for method in Accessibility.getFullAXTree DOMSnapshot.captureSnapshot DOM.getDocument; do
  prefix='browseruse_bench\.agents\.browser_use.*\[browser-use cdp-liveness-request\]'
  start_count="$(rg -c "${prefix} start method=${method}" "${log_path}" || true)"
  finish_count="$(rg -c "${prefix} finish method=${method}" "${log_path}" || true)"
  error_count="$(rg -c "${prefix} error method=${method}" "${log_path}" || true)"
  interrupted_count="$(rg -c "${prefix} interrupted method=${method}" "${log_path}" || true)"
  printf '%s start=%s finish=%s error=%s interrupted=%s\n' \
    "${method}" \
    "${start_count:-0}" \
    "${finish_count:-0}" \
    "${error_count:-0}" \
    "${interrupted_count:-0}"
done

printf '\n=== Probe responses that overtook pending CDP requests ===\n'
if ! rg -n '\[browser-use cdp-liveness\] sample=.*still_pending_after=\[[^]]' "${log_path}"; then
  printf 'No overtaking sample found.\n'
fi

exit "${run_status}"
