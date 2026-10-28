"""Build once, seal once. No per-file rehash on every subsequent qualification."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import os
import io
import subprocess
import sys
import tarfile
import time

import publication as p
import rehearse_routine_publication as fixture


def _build(source,output):
    source,output=Path(source).resolve(),Path(output).resolve()
    p.require(source.is_relative_to(p.ROOT/'outputs') and output.is_relative_to(p.ROOT/'outputs'),'local_build_only')
    env=p.legacy.clean_environment(dict(BASE_API_URL='http://104.248.236.93',MAIN_DOMAIN='www.wahojobs.com',
        AI_SUBDOMAIN='worksmarter.wahojobs.com',NEXTAUTH_URL='https://www.wahojobs.com',
        NEXTAUTH_SECRET='synthetic-local-build-only-not-production',NEXT_PUBLIC_WAHOJOBS_CANDIDATE_ENABLED='1',
        NODE_ENV='production',BUILD_SCOPE_TRACE=str(output/'public-build-network.jsonl'),
        NODE_OPTIONS='--require "'+fixture.SCOPE.as_posix()+'"'))
    command=[fixture.NODE,str(fixture.DEPS/'next/dist/bin/next'),'build']
    # Git archive applies this installation's checkout attributes (including
    # Windows line endings). Compare the exact archive, not raw Git blob bytes.
    raw_archive=p.legacy.git(fixture.WEBSITE,'archive',p.legacy.git(fixture.WEBSITE,'rev-parse','HEAD').decode().strip())
    with tarfile.open(fileobj=io.BytesIO(raw_archive)) as package:
        tree={member.name:p.digest(package.extractfile(member).read()) for member in package if member.isfile()}
    for relative,item in tree.items():
        raw=(source/relative).read_bytes()
        p.require(p.digest(raw)==item,'build_source_not_exact')
    binding=p.digest(p.encoded(dict(tree=tree,command=command,environment={k:env[k] for k in (
        'BASE_API_URL','MAIN_DOMAIN','AI_SUBDOMAIN','NEXTAUTH_URL','NEXTAUTH_SECRET','NEXT_PUBLIC_WAHOJOBS_CANDIDATE_ENABLED','NODE_ENV','NODE_OPTIONS')},
        tools={path:p.digest(Path(path).read_bytes()) for path in (fixture.NODE,str(fixture.DEPS/'next/dist/bin/next'),str(fixture.DEPS/'next/package.json'),str(fixture.SCOPE))})))
    def files():return [f for f in sorted((source/'.next').rglob('*')) if f.is_file()
        and not set(f.relative_to(source/'.next').parts)&{'cache','types'} and f.name!='trace']
    def identities():
        return {str(f.relative_to(source)).replace('\\','/'):[f.stat().st_size,f.stat().st_mtime_ns,f.stat().st_ctime_ns,f.stat().st_ino] for f in files()}
    tick=time.monotonic();completed=output/'next-build-result.json'
    if completed.exists():
        previous=p.read(completed)
        p.require(previous['binding']==binding and previous['owned_output_identities']==identities(),'completed_build_context_or_output_changed')
        built=previous['seconds']
    else:
        subprocess.run(command,cwd=source,env=env,check=True,timeout=600)
        built=time.monotonic()-tick
        p.once(completed,dict(binding=binding,at=p.now(),seconds=built,owned_output_identities=identities(),command_exit=0))
    artifact=output/'website-build.tar'
    if (output/'build-manifest.json').exists():
        p.require(p.read(output/'build-manifest.json')=={str(artifact):p.digest(artifact.read_bytes())},'sealed_build_changed')
        return # Original build/seal timestamps and artifact are retained.
    listing=output/'runtime-build-files.txt';listing.write_text('\n'.join(str(f.relative_to(source)).replace('\\','/') for f in files())+'\n',encoding='utf-8')
    seal_tick=time.monotonic()
    if artifact.exists():
        # A previous LOCAL packager must have exited before this check's lock
        # can be reused. Keep its partial output and the completed Next result.
        os.rename(artifact,output/('website-build-'+str(time.time_ns())+'.failed.tar'))
    entries=files()
    # Avoid serial Windows file/owner lookups. Read independent output files in
    # bounded batches; retain actual bytes, not metadata-only qualification.
    with tarfile.open(artifact,'w:') as package,ThreadPoolExecutor(max_workers=16) as pool:
        for offset in range(0,len(entries),32):
            batch=entries[offset:offset+32]
            for path,raw in zip(batch,pool.map(lambda path:path.read_bytes(),batch)):
                member=tarfile.TarInfo(path.relative_to(source).as_posix());member.size=len(raw);member.mode=0o644
                package.addfile(member,io.BytesIO(raw))
    p.require(p.read(completed)['owned_output_identities']==identities(),'build_output_changed_during_seal')
    count=0
    with tarfile.open(artifact,'r:') as package:
        for member in package:
            path=Path(member.name)
            p.require(not path.is_absolute() and '..' not in path.parts and (member.isdir() or member.isfile()),'unsafe_build_archive')
            count+=member.isfile()
    p.require((source/'.next/BUILD_ID').read_text().strip(),'production_build_id_missing')
    p.once(output/'build-manifest.json',{str(artifact):p.digest(artifact.read_bytes())})
    p.once(output/'build-timing.json',dict(build_seconds=built,seal_seconds=time.monotonic()-seal_tick,files=count,at=p.now(),
        original_build_result=str(completed),build_reused=completed.exists() and 'previous' in locals(),parallel_reads=16))


def build(source,output):
    output=Path(output).resolve()
    p.require(output.is_relative_to(p.ROOT/'outputs'),'local_build_only')
    with p.legacy.file_lock(output/'build-driver.lock'):
        return _build(source,output)


if __name__=='__main__':build(*sys.argv[1:])
