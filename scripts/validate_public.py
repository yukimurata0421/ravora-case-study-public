#!/usr/bin/env python3
"""Validate the declared public payload without consulting a private checkout."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote,urlsplit

ROOT=Path(__file__).resolve().parents[1]
CONTROL={'publication/source-map.json','publication/validation.json'}
PUBLIC_MOVIES = {
    'docs/media/ray-upper-lip-conversation.mp4',
    'docs/media/ray-mouth-before-after-comparison.mp4',
}


def payload_files():
    for parent,dirs,files in os.walk(ROOT):
        dirs[:]=[name for name in dirs if name not in ('.git','__pycache__','build','dist')
                 and not name.startswith('.venv') and not name.endswith('.egg-info')]
        if any((Path(parent)/name).is_symlink() for name in dirs):raise ValueError('SYMLINK_DIRECTORY_IN_PAYLOAD')
        for name in files:
            if Path(parent)==ROOT and name=='.git':continue
            if not name.endswith(('.pyc','.pyo')):yield Path(parent)/name


def main():
    manifest=json.loads((ROOT/'publication/source-map.json').read_text())
    validation=json.loads((ROOT/'publication/validation.json').read_text())
    if manifest.get('schema_version')!=1 or manifest.get('kind')!='allowlisted-case-study-port':raise ValueError('INVALID_PORT_MANIFEST')
    if not isinstance(validation,dict) or not str(validation.get('status','')).startswith('PASS_') or not isinstance(validation.get('checks'),dict):raise ValueError('INVALID_LOCAL_VALIDATION_RECORD')
    if validation.get('included_private_databases_or_recordings') is not False or validation.get('new_inference_api_calls')!=0:raise ValueError('UNSAFE_LOCAL_VALIDATION_RECORD')
    entries=manifest['files'];declared={entry['path'] for entry in entries}|CONTROL
    if len(declared)!=len(entries)+len(CONTROL):raise ValueError('DUPLICATE_MANIFEST_PATH')
    observed={str(path.relative_to(ROOT)) for path in payload_files()}
    if declared!=observed:raise ValueError('PUBLIC_ALLOWLIST_MISMATCH')
    forbidden=[re.compile('/'+'home'+r'/[^\s]+'),re.compile('/'+'mnt'+r'/[^\s]+'),
        re.compile('/'+'run'+r'/stream-v3-control'),re.compile(r'ravora-20\d{6}-[a-z0-9]+'),
        re.compile(r'\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b')]
    for entry in entries:
        path=ROOT/entry['path']
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):raise ValueError('UNSAFE_PAYLOAD_PATH')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:raise ValueError('PUBLIC_HASH_MISMATCH:'+entry['path'])
    for path in payload_files():
        relative=str(path.relative_to(ROOT))
        if relative in PUBLIC_MOVIES or relative == 'docs/media/voice-audition.m4a':
            with path.open('rb') as media:
                header = media.read(12)
                if header[4:8] != b'ftyp' or (path.suffix == '.m4a' and header[8:12] != b'M4A '):
                    raise ValueError('INVALID_PUBLIC_MEDIA_CONTAINER')
            continue
        if path.suffix.lower() in ('.sqlite3','.db','.wav','.m4a','.mp3','.vrm','.vroid','.mp4','.webm','.log') or path.name in ('.env','settings.json'):raise ValueError('PRIVATE_ARTIFACT_IN_PAYLOAD')
        if path.suffix.lower()=='.png':continue
        text=path.read_text()
        if any(pattern.search(text) for pattern in forbidden):raise ValueError('PRIVATE_DEFAULT_IN_PAYLOAD:'+relative)
        if path.suffix=='.md':
            if re.search('[\u3040-\u30ff\u3400-\u9fff]',text):raise ValueError('PUBLIC_DOC_LANGUAGE:'+relative)
            for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)',text):
                parsed=urlsplit(target.strip('<>'))
                if parsed.scheme or not parsed.path:continue
                linked=(path.parent/unquote(parsed.path)).resolve()
                if not linked.is_relative_to(ROOT) or not linked.exists():raise ValueError('BROKEN_PUBLIC_LINK:'+relative)
    result={'status':'PASS_PUBLIC_PAYLOAD','files':len(observed),'hashes_checked':len(entries),
        'unexpected_files':0,'private_defaults_found':0,'broken_local_links':0}
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,OSError) as exc:
        print(str(exc),file=sys.stderr);raise SystemExit(1)
