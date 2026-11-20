"""Server-side speaker identity and the private developer memory namespace."""
import hashlib


DEVELOPER_PLATFORMS = ('developer', 'developer-local', 'developer-mock')


def viewer_key(platform, author_id):
    return platform + ':v2:' + hashlib.sha256(author_id.encode()).hexdigest()[:40]


def developer_keys(identifier):
    return tuple(viewer_key(platform, identifier) for platform in DEVELOPER_PLATFORMS)


def memory_source_filter(viewer):
    if viewer.partition(':')[0] in DEVELOPER_PLATFORMS:
        return """ AND EXISTS(SELECT 1 FROM memory_sources x
            JOIN developer_memory_sources d ON d.event_id=x.event_id
            WHERE x.memory_id=m.memory_id AND d.owner_key=m.viewer_key)"""
    return ' AND EXISTS(SELECT 1 FROM memory_sources x WHERE x.memory_id=m.memory_id)'
