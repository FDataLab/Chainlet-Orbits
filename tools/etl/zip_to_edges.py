#!/usr/bin/env python3
"""
ZIP → (block_times/ + edge_dir/inedges/ + edge_dir/outedges/) converter.

- Streams JSON out of each ZIP (no full unzip; memory-safe with ijson).
- Produces:
    <work_dir>/block_times/block_times.jsonl
    <work_dir>/edge_dir/inedges/<bucket_index>_in.csv
    <work_dir>/edge_dir/outedges/<bucket_index>_out.csv
  where bucket_index = (block_height // bucket_size) * bucket_size

- Line format for *_in.csv and *_out.csv (TAB-separated):
    <block_height>    <tx_hash>    -    <addr1>    <value1>    <addr2>    <value2> ...

- Writes per-run stats to <work_dir>/stats/run_stats.csv and optional plots
  under <work_dir>/plots/.

Requirements: ijson, tqdm. 
"""

VERSION = "2025-08-17c (flat-array only; single blocks bar)"

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from glob import glob
from typing import Dict, Generator, List, Optional, Tuple

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    def tqdm(x, **kwargs):  # type: ignore
        return x

try:
    import ijson
except ImportError:
    print("ERROR: ijson is required. Install with: pip install ijson tqdm", file=sys.stderr)
    sys.exit(1)


# ----------------------------- Utilities & Types ----------------------------- #

@dataclass
class ZipMeta:
    name: str
    size: int
    mtime: float

def get_zip_meta(path: str) -> ZipMeta:
    st = os.stat(path)
    return ZipMeta(name=os.path.basename(path), size=st.st_size, mtime=st.st_mtime)

def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def open_text_from_zip(zf: zipfile.ZipFile, member: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(zf.open(member), encoding="utf-8", errors="ignore")

def parse_zip_block_range(zip_basename: str) -> Optional[Tuple[int, int]]:
    m = re.search(r'_(\d+)-(\d+)\.zip$', zip_basename)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


# ----------------------------- Streaming readers ----------------------------- #
# IMPORTANT: Your dataset uses FLAT ARRAYS. We lock to 'item' so we do not double-read.

def iter_blocks_flat(zf: zipfile.ZipFile, member: str, logger: logging.Logger) -> Generator[dict, None, None]:
    # Detect format by reading first few bytes
    with zf.open(member) as fh:
        first_bytes = fh.read(100)
    
    is_json_array = first_bytes.strip().startswith(b'[')
    # Log BEFORE yielding starts (won't interfere with tqdm)
    # logger.info(f"blocks.json format: {'JSON array' if is_json_array else 'NDJSON'}")
    
    if is_json_array:
        with open_text_from_zip(zf, member) as txt:
            for obj in ijson.items(txt, "item"):
                if isinstance(obj, dict):
                    yield obj
    else:
        with zf.open(member) as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", errors="ignore"))
                    if isinstance(obj, dict):
                        yield obj
                except Exception:
                    continue

def iter_transactions_flat(zf: zipfile.ZipFile, member: str, logger: logging.Logger) -> Generator[dict, None, None]:
    # Detect format by reading first few bytes
    with zf.open(member) as fh:
        first_bytes = fh.read(100)
        
    # Check if it starts with '[' (JSON array) or '{' (NDJSON)
    if first_bytes.strip().startswith(b'['):
        # JSON array format - no logging here
        with open_text_from_zip(zf, member) as txt:
            for obj in ijson.items(txt, "item"):
                if isinstance(obj, dict):
                    yield obj
    else:
        # NDJSON format - no logging here
        with zf.open(member) as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", errors="ignore"))
                    if isinstance(obj, dict):
                        yield obj
                except Exception:
                    continue


# ----------------------------- Field extractors ------------------------------ #

def parse_block_number(obj: dict) -> Optional[int]:
    for k in ("number", "block_number"):
        v = obj.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None

def parse_block_timestamp(obj: dict) -> Optional[int]:
    for k in ("timestamp", "block_timestamp"):
        v = obj.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None

def parse_txid(obj: dict) -> Optional[str]:
    for k in ("hash", "txid", "id"):
        v = obj.get(k)
        if isinstance(v, str) and v:
            return v
    return None

def parse_value(x) -> int:
    if isinstance(x, int) and x >= 0:
        return x
    if isinstance(x, str) and x.isdigit():
        return int(x)
    return 0

def parse_addrs_from_part(part: dict) -> List[str]:
    if isinstance(part.get("addresses"), list):
        return [a for a in part["addresses"] if isinstance(a, str) and a]
    a = part.get("address")
    return [a] if isinstance(a, str) and a else []

def parse_parts(part_list: Optional[list]) -> List[Tuple[str, int]]:
    if not isinstance(part_list, list):
        return []
    out: List[Tuple[str, int]] = []
    for part in part_list:
        if not isinstance(part, dict):
            continue
        addrs = parse_addrs_from_part(part)
        val = parse_value(part.get("value"))
        if val == 0:
            val = parse_value(part.get("prev_value"))
        for a in addrs:
            a2 = a.strip()
            if a2:
                out.append((a2, val))
    return out


# ----------------------------- Buffered edge writers ------------------------- #

class EdgeWriter:
    """
    Buffers lines per bucket file to reduce open/close overhead.
    Keeps at most `max_buffers` buckets in memory; flushes automatically.
    """
    def __init__(self, base_dir: str, suffix: str, flush_every: int = 2000, max_buffers: int = 64):
        self.base_dir = base_dir
        self.suffix = suffix  # "in" or "out"
        self.flush_every = flush_every
        self.max_buffers = max_buffers
        self.buffers: Dict[str, List[str]] = {}
        self.counts: Dict[str, int] = {}
        ensure_dir(base_dir)

    def _path(self, bucket_index: int) -> str:
        return os.path.join(self.base_dir, f"{bucket_index}_{self.suffix}.csv")

    def write(self, bucket_index: int, block_no: int, txid: str, pairs: List[Tuple[str, int]]):
        path = self._path(bucket_index)
        line: List[str] = [str(block_no), txid, "-"]
        for a, v in pairs:
            line.append(a)
            line.append(str(int(v)))
        s = "\t".join(line) + "\n"
        buf = self.buffers.setdefault(path, [])
        buf.append(s)
        self.counts[path] = self.counts.get(path, 0) + 1
        if len(buf) >= self.flush_every:
            self._flush_path(path)
        if len(self.buffers) > self.max_buffers:
            # flush an arbitrary path to keep memory/FDs in check
            self._flush_path(next(iter(self.buffers)))

    def _flush_path(self, path: str):
        buf = self.buffers.get(path, [])
        if not buf:
            return
        with open(path, "a", encoding="utf-8") as fh:
            fh.writelines(buf)
        self.buffers[path] = []

    def flush_all(self):
        for p in list(self.buffers.keys()):
            self._flush_path(p)


class BlockTimesWriter:
    """Append-only writer for block_times.jsonl with de-dup across runs."""
    def __init__(self, block_times_path: str, logger: logging.Logger, enabled: bool = True):
        self.enabled = enabled
        self.path = block_times_path
        self.logger = logger
        self._seen: set[int] = set()
        if not self.enabled:
            return
        ensure_dir(os.path.dirname(self.path))
        if os.path.exists(self.path):
            # Stream load to avoid big memory spikes
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            obj = json.loads(line)
                            n = obj.get("number") or obj.get("block_number")
                            if isinstance(n, int):
                                self._seen.add(n)
                        except Exception:
                            continue
                self.logger.info("Loaded %d existing block_times entries.", len(self._seen))
            except Exception as e:
                self.logger.warning("Could not pre-load existing block_times: %s", e)
        self._fh = open(self.path, "a", encoding="utf-8") if self.enabled else None

    def write(self, block_no: int, ts: int):
        if not self.enabled:
            return
        if block_no in self._seen:
            return
        obj = {"number": block_no, "timestamp": ts}
        assert self._fh is not None
        self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self._seen.add(block_no)

    def close(self):
        if not self.enabled:
            return
        try:
            assert self._fh is not None
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass


def bucket_index(block_no: int, bucket_size: int) -> int:
    return (block_no // bucket_size) * bucket_size


# ----------------------------- Manifest & Stats ------------------------------ #

def load_manifest(path: str) -> Dict[str, dict]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}

def save_manifest(path: str, data: Dict[str, dict]) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)

def append_stats_row(stats_csv: str, headers: List[str], row: List):
    new = not os.path.exists(stats_csv)
    ensure_dir(os.path.dirname(stats_csv))
    with open(stats_csv, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(headers)
        w.writerow(row)

def try_make_plots(stats_csv: str, out_dir: str, logger: logging.Logger):
    try:
        import matplotlib.pyplot as plt
    except Exception as e:
        logger.warning("Plotting skipped (matplotlib not available): %s", e)
        return
    rows: List[dict] = []
    try:
        with open(stats_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(r)
    except Exception as e:
        logger.warning("Could not read stats for plotting: %s", e)
        return
    if not rows:
        return
    xs = list(range(1, len(rows) + 1))
    txs = [int(r.get("tx_count", "0")) for r in rows]
    in_refs = [int(r.get("in_addr_refs", "0")) for r in rows]
    out_refs = [int(r.get("out_addr_refs", "0")) for r in rows]
    ensure_dir(out_dir)
    try:
        plt.figure(); plt.plot(xs, txs); plt.xlabel("ZIP index"); plt.ylabel("Transactions parsed")
        plt.title("Transactions per ZIP"); plt.tight_layout()
        plt.savefig(os.path.join(out_dir, "tx_per_zip.png")); plt.close()
    except Exception as e:
        logger.warning("Failed to save tx_per_zip.png: %s", e)
    try:
        plt.figure(); plt.plot(xs, in_refs, label="in addr refs"); plt.plot(xs, out_refs, label="out addr refs")
        plt.xlabel("ZIP index"); plt.ylabel("Address refs"); plt.title("Address references per ZIP")
        plt.legend(); plt.tight_layout()
        plt.savefig(os.path.join(out_dir, "refs_per_zip.png")); plt.close()
    except Exception as e:
        logger.warning("Failed to save refs_per_zip.png: %s", e)


# ----------------------------- Per-ZIP processing ---------------------------- #

def process_zip(zip_path: str,
                block_writer: BlockTimesWriter,
                in_writer: EdgeWriter,
                out_writer: EdgeWriter,
                bucket_size: int,
                logger: logging.Logger) -> dict:
    zmeta = get_zip_meta(zip_path)
    logger.info("Processing ZIP: %s (%.2f GB) | %s", zmeta.name, zmeta.size / (1024**3), VERSION)

    tx_count = 0
    in_addr_refs = 0
    out_addr_refs = 0
    uniq_blocks: set[int] = set()

    with zipfile.ZipFile(zip_path) as zf:
        blocks_member = next((n for n in zf.namelist() if n.endswith("blocks.json")), None)
        tx_member     = next((n for n in zf.namelist() if n.endswith("transactions.json")), None)

        # Determine expected block count (nice tqdm %)
        expected_blocks = None
        zr = parse_zip_block_range(zmeta.name)
        if zr:
            expected_blocks = max(0, zr[1] - zr[0] + 1)

        # Single pass over blocks.json: write block_times (if enabled) and sum tx_count for tqdm total
        sum_tx_count = 0
        if blocks_member:
            blocks_iter = iter_blocks_flat(zf, blocks_member, logger)
            pbar = tqdm(desc=f"[{zmeta.name}] blocks", total=expected_blocks, disable=not sys.stdout.isatty())
            for blk in blocks_iter:
                pbar.update(1)
                bn = parse_block_number(blk)
                ts = parse_block_timestamp(blk)
                if bn is None or ts is None:
                    continue
                block_writer.write(bn, ts)
                tc = blk.get("transaction_count")
                if isinstance(tc, int):
                    sum_tx_count += tc
            pbar.close()

        if not tx_member:
            logger.warning("No transactions.json in %s", zmeta.name)
            return {
                "zip_name": zmeta.name,
                "zip_size": zmeta.size,
                "tx_count": tx_count,
                "in_addr_refs": in_addr_refs,
                "out_addr_refs": out_addr_refs,
                "unique_blocks": 0,
            }

        with zf.open(tx_member) as fh:
            first_bytes = fh.read(100)
        tx_format = "JSON array" if first_bytes.strip().startswith(b'[') else "NDJSON"
        #logger.info(f"transactions.json format: {tx_format}")
        
        for tx in tqdm(
            iter_transactions_flat(zf, tx_member, logger),
            desc=f"[{zmeta.name}] tx",
            total=(sum_tx_count or None),
            disable=not sys.stdout.isatty()
        ):
            txid = parse_txid(tx)
            bno = parse_block_number(tx)
            if not txid or bno is None:
                continue

            ins  = parse_parts(tx.get("inputs"))
            outs = parse_parts(tx.get("outputs"))

            bkt = bucket_index(bno, bucket_size)
            in_writer.write(bkt,  bno, txid, ins)
            out_writer.write(bkt, bno, txid, outs)

            tx_count += 1
            in_addr_refs  += len(ins)
            out_addr_refs += len(outs)
            uniq_blocks.add(bno)

    # flush buffered I/O
    in_writer.flush_all()
    out_writer.flush_all()

    return {
        "zip_name": zmeta.name,
        "zip_size": zmeta.size,
        "tx_count": tx_count,
        "in_addr_refs": in_addr_refs,
        "out_addr_refs": out_addr_refs,
        "unique_blocks": len(uniq_blocks),
    }


# ----------------------------- CLI / Driver ---------------------------------- #

def build_logger(log_dir: str) -> logging.Logger:
    ensure_dir(log_dir)
    logger = logging.getLogger("zip_to_edges")
    logger.setLevel(logging.INFO)
    logger.handlers = []
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    ch = logging.StreamHandler(sys.stdout); ch.setLevel(logging.INFO); ch.setFormatter(fmt); logger.addHandler(ch)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    fh = logging.FileHandler(os.path.join(log_dir, f"zip_to_edges_{ts}.log"), encoding="utf-8")
    fh.setLevel(logging.INFO); fh.setFormatter(fmt); logger.addHandler(fh)
    return logger


def load_manifest(path: str) -> Dict[str, dict]:
    if not os.path.exists(path): return {}
    try:
        with open(path, "r", encoding="utf-8") as fh: return json.load(fh)
    except Exception: return {}

def save_manifest(path: str, data: Dict[str, dict]) -> None:
    ensure_dir(os.path.dirname(path)); tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh: json.dump(data, fh, indent=2)
    os.replace(tmp, path)

def append_stats_row(stats_csv: str, headers: List[str], row: List):
    new = not os.path.exists(stats_csv); ensure_dir(os.path.dirname(stats_csv))
    with open(stats_csv, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f); 
        if new: w.writerow(headers)
        w.writerow(row)

def try_make_plots(stats_csv: str, out_dir: str, logger: logging.Logger):
    try:
        import matplotlib.pyplot as plt
    except Exception as e:
        logger.warning("Plotting skipped (matplotlib not available): %s", e)
        return
    rows: List[dict] = []
    try:
        with open(stats_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader: rows.append(r)
    except Exception as e:
        logger.warning("Could not read stats for plotting: %s", e); return
    if not rows: return
    xs = list(range(1, len(rows) + 1))
    txs = [int(r.get("tx_count", "0")) for r in rows]
    in_refs = [int(r.get("in_addr_refs", "0")) for r in rows]
    out_refs = [int(r.get("out_addr_refs", "0")) for r in rows]
    ensure_dir(out_dir)
    try:
        plt.figure(); plt.plot(xs, txs); plt.xlabel("ZIP index"); plt.ylabel("Transactions parsed")
        plt.title("Transactions per ZIP"); plt.tight_layout(); plt.savefig(os.path.join(out_dir, "tx_per_zip.png")); plt.close()
    except Exception as e:
        logger.warning("Failed to save tx_per_zip.png: %s", e)
    try:
        plt.figure(); plt.plot(xs, in_refs, label="in addr refs"); plt.plot(xs, out_refs, label="out addr refs")
        plt.xlabel("ZIP index"); plt.ylabel("Address refs"); plt.title("Address references per ZIP")
        plt.legend(); plt.tight_layout(); plt.savefig(os.path.join(out_dir, "refs_per_zip.png")); plt.close()
    except Exception as e:
        logger.warning("Failed to save refs_per_zip.png: %s", e)


def main():
    ap = argparse.ArgumentParser(description="Convert Bitcoin ZIP exports into block_times + edge buckets.")
    ap.add_argument("--zip-dir", required=True, help="Directory containing export_blocks_and_transactions_*.zip")
    ap.add_argument("--work-dir", required=True, help="Output base dir (will create block_times/, edge_dir/, etc.)")
    ap.add_argument("--glob", default="export_blocks_and_transactions_*.zip",
                    help="Glob to select ZIPs (default: export_blocks_and_transactions_*.zip).")
    ap.add_argument("--bucket-size", type=int, default=100, help="Block bucket size for edge files (default 100).")
    ap.add_argument("--force", action="store_true", help="Re-process ZIPs even if manifest says done.")
    ap.add_argument("--skip-block-times", action="store_true",
                    help="Do not write block_times (useful when parallelizing across many ZIPs).")
    args = ap.parse_args()

    zip_dir = os.path.abspath(args.zip_dir)
    work_dir = os.path.abspath(args.work_dir)
    bucket_size = args.bucket_size

    # Layout
    block_times_path = os.path.join(work_dir, "block_times", "block_times.jsonl")
    in_dir  = os.path.join(work_dir, "edge_dir", "inedges")
    out_dir = os.path.join(work_dir, "edge_dir", "outedges")
    log_dir   = os.path.join(work_dir, "logs")
    cache_dir = os.path.join(work_dir, "cache")
    stats_dir = os.path.join(work_dir, "stats")
    plots_dir = os.path.join(work_dir, "plots")
    manifest_path = os.path.join(cache_dir, "manifest.json")
    stats_csv     = os.path.join(stats_dir, "run_stats.csv")

    for d in (in_dir, out_dir, log_dir, cache_dir, stats_dir, plots_dir, os.path.dirname(block_times_path)):
        ensure_dir(d)

    logger = build_logger(log_dir)

    logger.info("zip_to_edges.py VERSION: %s", VERSION)

    # Discover ZIPs
    pattern = os.path.join(zip_dir, args.glob)
    zips = sorted(glob(pattern))
    if not zips:
        logger.error("No ZIPs matching %s", pattern)
        sys.exit(2)

    # Load manifest cache
    manifest = load_manifest(manifest_path)

    # Writers
    bt_writer = BlockTimesWriter(block_times_path, logger, enabled=(not args.skip_block_times))

    # Stats CSV header
    headers = ["zip_name", "zip_size", "tx_count", "in_addr_refs", "out_addr_refs", "unique_blocks"]

    processed = 0
    start = time.time()
    for zp in tqdm(zips, desc="Processing ZIPs", disable=not sys.stdout.isatty()):
        meta = get_zip_meta(zp)
        key = meta.name
        if not args.force and key in manifest:
            prev = manifest[key]
            if prev.get("size") == meta.size and abs(prev.get("mtime", 0) - meta.mtime) < 1:
                logger.info("Skipping already-processed ZIP: %s", meta.name)
                continue

        in_writer  = EdgeWriter(in_dir,  "in")
        out_writer = EdgeWriter(out_dir, "out")

        stats = process_zip(zp, bt_writer, in_writer, out_writer, bucket_size, logger)
        append_stats_row(stats_csv, headers, [stats[h] for h in headers])

        manifest[key] = {"size": meta.size, "mtime": meta.mtime, "stats": stats}
        save_manifest(manifest_path, manifest)
        processed += 1

    bt_writer.close()

    dur = time.time() - start
    logger.info("Done. Processed %d ZIP(s) in %.1f sec.", processed, dur)

    # Optional plots
    try_make_plots(stats_csv, plots_dir, logger)


if __name__ == "__main__":
    main()
