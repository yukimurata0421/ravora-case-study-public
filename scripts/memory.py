#!/usr/bin/env python3
"""Resolve a provider-authenticated viewer in a local studio snapshot."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ravora.conversation import MemoryStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['viewer'])
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--platform', choices=['twitch', 'youtube'], required=True)
    parser.add_argument('--scope', choices=['cloud', 'offline'], default='cloud')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--id', dest='author_id')
    group.add_argument('--name')
    args = parser.parse_args()
    store = MemoryStore.existing(args.database, readonly=True)
    viewer = store.find_viewer(args.platform, scope=args.scope, author_id=args.author_id, name=args.name)
    print(json.dumps({'matched': bool(viewer), 'viewer_key': viewer}))


if __name__ == '__main__':
    main()
