"""Extract downloaded Ninapro DB6 .zip files to data/ninapro_db6/extracted/.

Skips macOS resource-fork junk (__MACOSX). Idempotent.
Usage: python scripts/extract_db6.py [data/ninapro_db6]
"""
import glob
import os
import sys
import zipfile


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "ninapro_db6")
    out = os.path.join(src, "extracted")
    os.makedirs(out, exist_ok=True)
    zips = sorted(glob.glob(os.path.join(src, "*.zip")))
    if not zips:
        print(f"no .zip files under {src}")
        return
    total = 0
    for zp in zips:
        with zipfile.ZipFile(zp) as z:
            mats = [n for n in z.namelist()
                    if n.lower().endswith(".mat") and not n.startswith("__MACOSX")]
            for n in mats:
                z.extract(n, out)
            total += len(mats)
            print(f"{os.path.basename(zp)}: extracted {len(mats)} .mat")
    print(f"done -> {out}  ({total} .mat files)")


if __name__ == "__main__":
    main()
