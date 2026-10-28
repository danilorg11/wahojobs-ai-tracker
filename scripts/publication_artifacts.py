"""Seal a successful build once; subsequent checks hash one contiguous artifact.

Retains the original receipt/log/date. Native tar avoids per-file Python I/O on
Windows; its bytes are then checked against the ORIGINAL qualified manifest.
This is packaging, not a new build or a restamped qualification.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time

import publication as p


def seal(receipt_path,manifest_path,source,output):
    receipt_path,manifest_path,source,output=map(Path,(receipt_path,manifest_path,source,output))
    source=source.resolve();output=output.resolve()
    p.require(output.is_relative_to(p.ROOT/'outputs'),'local_packaging_only')
    receipt=p.read(receipt_path);p.require(receipt['passed'],'only_successful_original_build_may_be_sealed')
    p.require(p.digest(Path(receipt['log']).read_bytes())==receipt['log_sha256'],'original_build_log_changed')
    for category in ('files','tools','outputs'):
        p.require(all(p.digest(Path(path).read_bytes())==value for path,value in receipt[category].items()),'original_build_inputs_changed')
    expected={str(Path(path).relative_to(source)).replace('\\','/'):value for path,value in p.read(manifest_path).items()}
    # Mutable runtime tracing, caches and generated type declarations are not
    # deployment code and are intentionally not part of the sealed artifact.
    expected={k:v for k,v in expected.items() if k!='.next/trace' and not k.startswith('.next/types/')}
    artifact=output/'website-build.tar';listfile=output/'build-files.txt'
    p.once(output/'seal-intent.json',dict(original_receipt=str(receipt_path),sha256=p.digest(receipt_path.read_bytes()),at=p.now()))
    listfile.write_text('\n'.join(sorted(expected))+'\n',encoding='utf-8')
    tick=time.monotonic()
    subprocess.run(['tar','-cf',str(artifact),'-C',str(source),'-T',str(listfile)],check=True,shell=False,timeout=180)
    seen=set()
    with tarfile.open(artifact,'r:') as package:
        for member in package:
            p.require(member.isfile() and member.name in expected and member.name not in seen,'unexpected_build_archive_entry')
            with package.extractfile(member) as data:p.require(p.digest(data.read())==expected[member.name],'original_build_changed_before_seal')
            seen.add(member.name)
    p.require(seen==set(expected),'incomplete_build_archive')
    p.once(output/'sealed-build.json',{str(artifact):p.digest(artifact.read_bytes())})
    result=dict(original_build_receipt=str(receipt_path),original_started_at=receipt['started_at'],original_build_seconds=receipt['seconds'],
        archive=str(artifact),archive_sha256=p.digest(artifact.read_bytes()),files=len(seen),seconds=time.monotonic()-tick,
        rebuilt=False,original_evidence_restamped=False)
    p.once(output/'seal-result.json',result);return result


def verify(result_path,expected_receipt_hash):
    result=p.read(result_path);original=Path(result['original_build_receipt'])
    p.require(p.digest(original.read_bytes())==expected_receipt_hash,'original_build_receipt_changed')
    p.require(p.read(original)['passed'] and p.digest(Path(result['archive']).read_bytes())==result['archive_sha256'],'sealed_build_changed')
    return dict(valid=True,rebuilt=False,original_receipt=str(original),original_started_at=result['original_started_at'])


if __name__=='__main__':
    print(json.dumps(verify(*sys.argv[2:]) if sys.argv[1]=='verify' else seal(*sys.argv[1:]),ensure_ascii=False))
