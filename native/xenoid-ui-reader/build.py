#!/usr/bin/env python3
"""Build the embedded pipe-only reader with javac and Android Build Tools d8."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile
import zipfile

def main():
    p=argparse.ArgumentParser();p.add_argument('--javac',default='javac');p.add_argument('--d8',default='d8');p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    source=Path(__file__).with_name('StockUiServer.java')
    with tempfile.TemporaryDirectory(prefix='xenoid-ui-build-')as temporary:
        root=Path(temporary);classes=root/'classes';classes.mkdir();dex=root/'dex';dex.mkdir()
        subprocess.run([a.javac,'--release','8','-d',str(classes),str(source)],check=True)
        subprocess.run([a.d8,'--min-api','26','--output',str(dex),*[str(v)for v in sorted(classes.rglob('*.class'))]],check=True)
        with zipfile.ZipFile(a.output,'w',compression=zipfile.ZIP_DEFLATED)as archive:
            entry=zipfile.ZipInfo('classes.dex',(2026,1,1,0,0,0));entry.compress_type=zipfile.ZIP_DEFLATED;archive.writestr(entry,(dex/'classes.dex').read_bytes())
    print(hashlib.sha256(a.output.read_bytes()).hexdigest()+'  '+str(a.output))
if __name__=='__main__':main()
