"""Bounded failure metadata, without recorded audio, lyrics or private paths."""
from datetime import datetime, timezone

from storage import atomic_json, read_json


def record_failure(directory,title,progress,event):
    diagnostic=event.get('diagnostic',{})
    record={'at':datetime.now(timezone.utc).isoformat(),'title':title,
            'stage':diagnostic.get('stage',progress.phase),'percent':progress.snapshot()['percent'],
            'runtime':dict(progress.runtime)}
    for name in ('error_type','exit_code'):
        if name in diagnostic:record[name]=diagnostic[name]
    record['frames']=[{name:frame[name] for name in ('module','function','line') if name in frame}
                      for frame in diagnostic.get('frames',[])][-6:]
    path=directory/'audio_failures.json'
    try:
        previous=read_json(path,[])
        atomic_json(path,([record]+(previous if isinstance(previous,list) else []))[:20])
    except (OSError,ValueError):
        # Reporting a worker failure must still reach the UI on a full disk.
        pass
