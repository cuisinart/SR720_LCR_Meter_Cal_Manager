# SR715 / SR720 Calibration Backup Utility

Python utility for communicating with **Stanford Research Systems SR715 and SR720 LCR meters** over RS-232.

It's always a good idea to back up your calibration values for instruments like the SR715/720 which rely on a battery to save calibration constants in volatile memory. Always back up your values before replacing your internal sram retention battery.

This script helps you:
- Dump calibration-related values from the meter into CSV.
- Restore calibration values from a CSV file back to the meter.
- Compare live instrument values against a saved CSV baseline.

The main script is `Serial_SR720.py`.

## What The SR715/SR720 Are

The SR715 and SR720 are precision LCR meters used to measure inductance, capacitance, resistance, and related parameters. They expose a serial command interface for diagnostics and calibration data access.

This utility uses those serial commands to automate calibration data backup/restore workflows and reduce manual entry errors.

## Why This Utility Is Useful

- Creates a portable calibration snapshot (`dump` mode).
- Enables repeatable calibration restore (`restore` mode).
- Provides verification against a known-good baseline (`compare` mode).
- Produces log files and summary output for traceability.

## Requirements

- Python 3.8+ (recommended)
- `pyserial`

Install dependency:

```bash
pip install pyserial
```

## Quick Start

Run from this project directory:

```bash
python Serial_SR720.py --port COM5 dump
```

Default files:
- CSV: `sr720_cal.csv`
- Log: `sr720_cal.log`
- Compare report: `sr720_compare.csv`

## Command Usage

### Dump calibration data to CSV

```bash
python Serial_SR720.py --port COM5 dump --csv sr720_cal.csv --log sr720_cal.log
```

### Restore from CSV

```bash
python Serial_SR720.py --port COM5 restore --csv sr720_cal.csv --log sr720_cal.log
```

Dry-run (no writes sent):

```bash
python Serial_SR720.py --port COM5 restore --dry-run --csv sr720_cal.csv
```

### Compare live values to CSV

```bash
python Serial_SR720.py --port COM5 compare --csv sr720_cal.csv --report sr720_compare.csv --tol 1e-6
```

## CSV and Logging

The dump/restore CSV columns are:
- `key`
- `cmd_read`
- `cmd_write`
- `scope`
- `value`
- `notes`

The compare report CSV columns are:
- `key`
- `cmd_read`
- `expected`
- `actual`
- `status`
- `delta`
- `notes`

The script appends operational details to the log file and prints a mode summary to the terminal on completion.

## Important Operational Notes

- **Back up first**: always run `dump` before any restore operation.
- **Calibration write protection**: if writes fail, confirm the meter’s CAL jumper / calibration enable condition.
- **Floating-point readback**: small least-significant-digit differences can occur due to instrument quantization/formatting.

## Typical Workflow

1. Dump current calibration:
   - `python Serial_SR720.py --port COM5 dump --csv baseline.csv`
2. Make intended edits or use a reference CSV.
3. Restore:
   - `python Serial_SR720.py --port COM5 restore --csv baseline.csv`
4. Compare:
   - `python Serial_SR720.py --port COM5 compare --csv baseline.csv --report verify.csv`

## Troubleshooting

- `Could not open serial port`:
  - Verify port name (`COMx`), cabling, and that no other app is using the port.
- No instrument response:
  - Check baud/parity/stopbits settings and line termination (`--eol`).
- Write failures:
  - Check CAL write-enable conditions like the internal jumper and review `sr720_cal.log`.
