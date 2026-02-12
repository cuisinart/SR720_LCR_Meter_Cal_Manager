"""
SR715/SR720 calibration utility over RS232 by Andrew Kibler.

Purpose:
- Dump calibration-related values from the instrument to CSV.
- Restore values from CSV back to the instrument.
- Compare live readback values against a prior CSV export.

NOTE: This utility is intended for advanced users who understand the risks of modifying calibration constants. 
      Always ensure you have a backup of your instrument's state before performing restore operations.
      The floating point calbytes ($CFT) may have precision/formatting nuances; verify carefully after restore.

Code flow:
1. Parse CLI arguments and determine mode (`dump`, `restore`, `compare`).
2. Build `SerialCfg` from CLI parameters.
3. Execute the selected mode routine.
4. Log operational details and outcomes to the log file.
5. Gracefully handle serial-port open exceptions with actionable hints.
"""

import csv
import time
import argparse
import math
import sys
import os
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import serial  # if you get an ImportError, try running: pip install pyserial from your command line


# -----------------------------
# Manual-derived constants
# -----------------------------
DEFAULT_COM_PORT = "COM5"  # Adjust as needed for your system, e.g. "/dev/ttyUSB0" on Linux

CBT_MIN_I = 0
CBT_MAX_I = 94  # $CBT i range 0..94 (amplitude calbytes 0..94) :contentReference[oaicite:3]{index=3}

CRN_MIN = 0
CRN_MAX = 3     # 4 measurement ranges (0..3) per instrument; used for standard resistor cal range :contentReference[oaicite:4]{index=4}

CFT_MIN_I = 0
CFT_MAX_I = 120 # $CFT i range 0..120

CSV_HEADER = ["key", "cmd_read", "cmd_write", "scope", "value", "notes"]
COMPARE_HEADER = ["key", "cmd_read", "expected", "actual", "status", "delta", "notes"]


@dataclass
class SerialCfg:
    port: str
    baud: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout: float = 1.0
    write_timeout: float = 1.0
    eol: str = "\n"  # manual allows <cr> or <lf> terminator on RS232 :contentReference[oaicite:5]{index=5}


def open_serial(cfg: SerialCfg) -> serial.Serial:
    return serial.Serial(
        port=cfg.port,
        baudrate=cfg.baud,
        bytesize=cfg.bytesize,
        parity=cfg.parity,
        stopbits=cfg.stopbits,
        timeout=cfg.timeout,
        write_timeout=cfg.write_timeout,
    )


def _serial_open_hint(exc: Exception) -> str:
    msg = str(exc).lower()
    if "access is denied" in msg or "permissionerror" in msg:
        return "Access denied while opening the port. The port is likely already in use by another application."
    if "cannot find the file specified" in msg or "filenotfounderror" in msg:
        return "Requested serial port does not exist. Verify the COM port name and device connection."
    return "Failed to open serial port. Check port name, cable/device connection, and application permissions."


def log_serial_open_exception(log_path: str, cfg: SerialCfg, mode: str, exc: Exception):
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n--- {mode.upper()} ERROR {time.ctime()} ---\n")
        log.write(f"[FAIL] could not open port '{cfg.port}': {exc}\n")
        log.write(f"[HINT] {_serial_open_hint(exc)}\n")


def clear_com_buffer(ser: serial.Serial):
    # Send a bare newline to clear any partial command state on the instrument side.
    ser.write(b"\n")
    ser.flush()
    time.sleep(0.01)


def send_cmd(ser: serial.Serial, cmd: str, eol: str):
    clear_com_buffer(ser)
    ser.write((cmd + eol).encode("ascii", errors="ignore"))
    ser.flush()


def read_line(ser: serial.Serial) -> str:
    clear_com_buffer(ser)
    raw = ser.readline()
    if not raw:
        return ""
    return raw.decode("ascii", errors="ignore").strip()


def query(ser: serial.Serial, cmd: str, eol: str, settle: float = 0.05) -> str:
    ser.reset_input_buffer()
    send_cmd(ser, cmd, eol)
    time.sleep(settle)
    return read_line(ser)


def try_query(ser: serial.Serial, cmd: str, eol: str) -> Tuple[bool, str]:
    """Return (ok, response). ok=False if blank response or obvious error tokens."""
    resp = query(ser, cmd, eol=eol)
    if resp == "":
        return False, ""
    # SR715/720 indicates errors via status registers/ERR LED; text may still be blank.
    # We treat empty response as failure; otherwise accept.
    return True, resp


# -----------------------------
# Build the dump "items" list correctly
# -----------------------------
def build_dump_items() -> List[Dict[str, str]]:
    items = []

    # Identity / basic checks
    items.append(dict(key="IDN", cmd_read="*IDN?", cmd_write="", scope="global", notes="Instrument ID string"))
    #items.append(dict(key="TST", cmd_read="*TST?", cmd_write="", scope="global", notes="Self-test status (0=OK)"))  # :contentReference[oaicite:6]{index=6}

    # Frequency correction factor (ppm) :contentReference[oaicite:7]{index=7}
    items.append(dict(key="FRQ", cmd_read="$FRQ?", cmd_write="$FRQ", scope="global", notes="Frequency correction factor (ppm, ±10000, 0.1ppm)"))

    # Diagnostic mode enable/query :contentReference[oaicite:8]{index=8}
    #items.append(dict(key="DIA", cmd_read="$DIA?", cmd_write="$DIA", scope="global", notes="Diagnostic mode enable (0/1)"))

    # Rounding :contentReference[oaicite:9]{index=9}
    items.append(dict(key="RND", cmd_read="$RND?", cmd_write="$RND", scope="global", notes="Rounding (-1 auto, 0..3 fixed digits)"))

    # CBT amplitude calbytes indexed 0..94 :contentReference[oaicite:10]{index=10}
    for i in range(CBT_MIN_I, CBT_MAX_I + 1):
        items.append(dict(
            key=f"CBT[{i}]",
            cmd_read=f"$CBT? {i}",
            cmd_write=f"$CBT {i}",
            scope="global",
            notes="Amplitude calbyte (j=0..255)"
        ))

    # CMJ/CMN are range-scoped for standard resistor cal. Range selected via $CRN. :contentReference[oaicite:11]{index=11}
    for r in range(CRN_MIN, CRN_MAX + 1):
        # We record per-range CMJ/CMN; restore will set CRN then write these.
        items.append(dict(
            key=f"CMJ@R{r}",
            cmd_read=f"$CRN {r};$CMJ?",
            cmd_write=f"$CRN {r};$CMJ",
            scope=f"range:{r}",
            notes="Std resistor cal: major parameter (Ohms) for range r"
        ))
        items.append(dict(
            key=f"CMN@R{r}",
            cmd_read=f"$CRN {r};$CMN?",
            cmd_write=f"$CRN {r};$CMN",
            scope=f"range:{r}",
            notes="Std resistor cal: minor parameter (ppm) for range r"
        ))

    # CFT: manual says $CFT(?) i {,x} but does not specify i bounds in the excerpt;
    # we will probe indices during dump instead of hardcoding. :contentReference[oaicite:12]{index=12}
    # We'll add a placeholder entry; dump logic will expand it dynamically.
    for i in range(CFT_MIN_I, CFT_MAX_I + 1):
        items.append(dict(
            key=f"CFT[{i}]",
            cmd_read=f"$CFT? {i}",
            cmd_write=f"$CFT {i}",
            scope="global",
            notes="Floating point calbyte i = 0..120"
        ))

    

    return items


# -----------------------------
# Dump: expand CFT indices by probing
# -----------------------------
def probe_cft_indices(ser: serial.Serial, eol: str, max_probe: int = 256) -> List[Tuple[int, str]]:
    """
    Probe $CFT? i starting at 0 until we stop getting valid responses.
    Conservative stop rule: stop after N consecutive failures.
    """
    results = []
    consecutive_fail = 0
    for i in range(0, max_probe):
        ok, resp = try_query(ser, f"$CFT? {i}", eol)
        if ok:
            results.append((i, resp))
            consecutive_fail = 0
        else:
            consecutive_fail += 1
            # If we miss several in a row, assume we've passed valid range
            if consecutive_fail >= 5 and i > 10:
                break
    return results


def dump_to_csv(cfg: SerialCfg, csv_path: str, log_path: str):
    items = build_dump_items()

    with open(log_path, "a", encoding="utf-8") as log, open_serial(cfg) as ser:
        log.write(f"\n--- DUMP START {time.ctime()} ---\n")

        rows = []
        ok_count = 0
        fail_count = 0
        for it in items:

            cmd_read = it["cmd_read"]
            ok, val = try_query(ser, cmd_read, cfg.eol)

            if ok:
                ok_count += 1
                log.write(f"[ OK ] {it['key']} | {cmd_read} -> {val}\n")
                rows.append(dict(**it, value=val))
            else:
                fail_count += 1
                log.write(f"[FAIL] {it['key']} | {cmd_read} -> (no response)\n")
                rows.append(dict(**it, value=""))

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_HEADER)
            w.writeheader()
            w.writerows(rows)

        log.write(f"--- DUMP END ({len(rows)} rows) ---\n")
    return {"total": len(rows), "ok": ok_count, "fail": fail_count}


# -----------------------------
# Restore: replay CSV rows
# -----------------------------
def restore_from_csv(cfg: SerialCfg, csv_path: str, log_path: str, dry_run: bool = False):
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    with open(log_path, "a", encoding="utf-8") as log, open_serial(cfg) as ser:
        log.write(f"\n--- RESTORE START {time.ctime()} | dry_run={dry_run} ---\n")

        # sanity check
        ok, idn = try_query(ser, "*IDN?", cfg.eol)
        log.write(f"[INFO] *IDN? -> {idn if ok else '(no response)'}\n")

        failures = 0
        skip_count = 0
        dry_count = 0
        write_count = 0
        bad_verify_count = 0
        write_fail_count = 0
        for r in rows:
            key = (r.get("key") or "").strip()
            cmd_write = (r.get("cmd_write") or "").strip()
            value = (r.get("value") or "").strip()

            if cmd_write == "" or value == "":
                skip_count += 1
                log.write(f"[SKIP] {key} | cmd_write='{cmd_write}' value='{value}'\n")
                continue

            # cmd_write may be multi-command (e.g. "$CRN 2;$CMJ").
            # For those, append the value only to the last command.
            if ";" in cmd_write:
                parts = [p.strip() for p in cmd_write.split(";") if p.strip()]
            else:
                parts = [cmd_write]

            target = parts[-1]
            
              # $CBT i,j and $CFT i,x require a comma between index i and value.
            if target.startswith("$CBT ") or target.startswith("$CFT "):
                idx = target.split(None, 1)[1].strip() if len(target.split(None, 1)) > 1 else ""
                parts[-1] = f"{target},{value}" if idx else f"{target} {value}"
            else:
                parts[-1] = f"{target} {value}"

            full_cmd = ";".join(parts)

            try:
                if dry_run:
                    dry_count += 1
                    log.write(f"[DRY ] {key} | {full_cmd}\n")
                    continue

                send_cmd(ser, full_cmd, cfg.eol)
                write_count += 1
                time.sleep(0.05)

                # best-effort verify by re-querying if cmd_read exists
                cmd_read = (r.get("cmd_read") or "").strip()
                verify = ""
                status = "[ OK ]"
                if cmd_read:
                    ok2, verify = try_query(ser, cmd_read, cfg.eol)
                    if ok2:
                        verify = verify.strip()
                        is_match, _ = compare_values(value, verify, tol=0.0)
                        if not is_match:
                            status = "[BAD ]"
                            bad_verify_count += 1
                            failures += 1
                    else:
                        verify = "(no response)"

                log.write(f"{status} {key} | {full_cmd} | expected={value} verify={verify}\n")

            except Exception as e:
                write_fail_count += 1
                failures += 1
                log.write(f"[FAIL] {key} | {full_cmd} | error={e}\n")

        if failures > 0:
            log.write("--- Write failure detected.  Check CAL jumper enabled? ---\n")
        log.write(f"--- RESTORE END (failures={failures}) ---\n")
    return {
        "total": len(rows),
        "skipped": skip_count,
        "dry_run": dry_count,
        "writes": write_count,
        "bad_verify": bad_verify_count,
        "write_fail": write_fail_count,
        "failures": failures,
    }


def _to_float_or_none(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def compare_values(expected: str, actual: str, tol: float) -> Tuple[bool, str]:
    exp = expected.strip()
    act = actual.strip()

    if exp == act:
        return True, ""

    exp_num = _to_float_or_none(exp)
    act_num = _to_float_or_none(act)
    if exp_num is None or act_num is None:
        return False, ""

    delta = act_num - exp_num
    if math.isclose(exp_num, act_num, rel_tol=tol, abs_tol=tol):
        return True, f"{delta:.12g}"
    return False, f"{delta:.12g}"


def compare_from_csv(cfg: SerialCfg, csv_path: str, log_path: str, report_path: str, tol: float):
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    with open(log_path, "a", encoding="utf-8") as log, open_serial(cfg) as ser:
        log.write(f"\n--- COMPARE START {time.ctime()} | tol={tol} ---\n")

        ok, idn = try_query(ser, "*IDN?", cfg.eol)
        log.write(f"[INFO] *IDN? -> {idn if ok else '(no response)'}\n")

        results = []
        match_count = 0
        mismatch_count = 0
        skip_count = 0
        read_fail_count = 0

        for r in rows:
            key = (r.get("key") or "").strip()
            cmd_read = (r.get("cmd_read") or "").strip()
            expected = (r.get("value") or "").strip()
            notes = (r.get("notes") or "").strip()

            if cmd_read == "" or expected == "":
                skip_count += 1
                log.write(f"[SKIP] {key} | cmd_read='{cmd_read}' expected='{expected}'\n")
                results.append(dict(
                    key=key,
                    cmd_read=cmd_read,
                    expected=expected,
                    actual="",
                    status="skipped",
                    delta="",
                    notes=notes,
                ))
                continue

            ok_read, actual = try_query(ser, cmd_read, cfg.eol)
            if not ok_read:
                read_fail_count += 1
                log.write(f"[FAIL] {key} | {cmd_read} -> (no response)\n")
                results.append(dict(
                    key=key,
                    cmd_read=cmd_read,
                    expected=expected,
                    actual="",
                    status="read_fail",
                    delta="",
                    notes=notes,
                ))
                continue

            is_match, delta = compare_values(expected, actual, tol=tol)
            if is_match:
                match_count += 1
                log.write(f"[ OK ] {key} | expected={expected} actual={actual} delta={delta}\n")
                status = "match"
            else:
                mismatch_count += 1
                log.write(f"[DIFF] {key} | expected={expected} actual={actual} delta={delta}\n")
                status = "mismatch"

            results.append(dict(
                key=key,
                cmd_read=cmd_read,
                expected=expected,
                actual=actual,
                status=status,
                delta=delta,
                notes=notes,
            ))

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COMPARE_HEADER)
        w.writeheader()
        w.writerows(results)

    with open(log_path, "a", encoding="utf-8") as log:
        log.write(
            f"--- COMPARE END (match={match_count}, mismatch={mismatch_count}, "
            f"read_fail={read_fail_count}, skipped={skip_count}, total={len(results)}) ---\n"
        )
    return {
        "match": match_count,
        "mismatch": mismatch_count,
        "read_fail": read_fail_count,
        "skipped": skip_count,
        "total": len(results),
    }


def main():
    p = argparse.ArgumentParser(description="SR715/SR720 calibration constants dump/restore/compare over RS232. Default mode is dump.")
    p.add_argument("--port", default=DEFAULT_COM_PORT, help="COM5, /dev/ttyUSB0, etc.")
    p.add_argument("--baud", type=int, default=9600, help = "Baud rate (default 9600)")
    p.add_argument("--parity", default="N", choices=["N", "E", "O"], help = "Parity (default N=None)")
    p.add_argument("--stopbits", type=int, default=1, choices=[1, 2], help = "Number of stop bits (default 1)")
    p.add_argument("--timeout", type=float, default=1.0, help = "Timeout in seconds (default 1.0)")
    p.add_argument("--eol", default="\\n", help="Command terminator, e.g. \\n or \\r\\n")
    p.add_argument("--csv", default="sr720_cal.csv", help = "CSV file path for dump/restore input or compare baseline")
    p.add_argument("--log", default="sr720_cal.log", help = "Log file output name default sr720_cal.log")
    p.add_argument("--report", default="sr720_compare.csv", help="Compare report CSV output path")
    sub = p.add_subparsers(dest="mode")
    sub.add_parser("dump").set_defaults(mode="dump")
    r = sub.add_parser("restore")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(mode="restore")
    c = sub.add_parser("compare")
    c.add_argument("--tol", type=float, default=1e-6, help="Tolerance for numeric comparisons")
    c.set_defaults(mode="compare")
    p.set_defaults(mode="dump", dry_run=False, tol=1e-6)

    args = p.parse_args()
    eol = args.eol.encode("utf-8").decode("unicode_escape")

    if args.mode in ("restore", "compare") and not os.path.isfile(args.csv):
        p.error(f"CSV file not found for mode '{args.mode}': {args.csv}")

    cfg = SerialCfg(
        port=args.port,
        baud=args.baud,
        parity=args.parity,
        stopbits=args.stopbits,
        timeout=args.timeout,
        eol=eol,
    )

    try:
        if args.mode == "dump":
            stats = dump_to_csv(cfg, args.csv, args.log)
            print(
                f"[SUMMARY] dump total={stats['total']} ok={stats['ok']} fail={stats['fail']} "
                f"csv={args.csv} log={args.log}"
            )
        elif args.mode == "restore":
            stats = restore_from_csv(cfg, args.csv, args.log, dry_run=getattr(args, "dry_run", False))
            print(
                f"[SUMMARY] restore total={stats['total']} skipped={stats['skipped']} dry_run={stats['dry_run']} "
                f"writes={stats['writes']} bad_verify={stats['bad_verify']} write_fail={stats['write_fail']} "
                f"failures={stats['failures']} csv={args.csv} log={args.log}"
            )
        else:
            stats = compare_from_csv(cfg, args.csv, args.log, report_path=args.report, tol=getattr(args, "tol", 1e-6))
            print(
                f"[SUMMARY] compare total={stats['total']} match={stats['match']} mismatch={stats['mismatch']} "
                f"read_fail={stats['read_fail']} skipped={stats['skipped']} report={args.report} log={args.log}"
            )
    except serial.SerialException as exc:
        log_serial_open_exception(args.log, cfg, args.mode, exc)
        print(f"[ERROR] Could not open serial port '{cfg.port}'. {exc}")
        print(f"[HINT] {_serial_open_hint(exc)}")
        print(f"[INFO] See log: {args.log}")
        sys.exit(1)


if __name__ == "__main__":
    main()
