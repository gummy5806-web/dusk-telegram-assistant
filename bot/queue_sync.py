"""Sync exactly validated assistant handoff files, preserving unrelated edits."""
from pathlib import Path
import logging
import subprocess
import queue_store
import semantic_search_jobs
import ai_usage
logger=logging.getLogger(__name__)


def _git(command,root):
    try:
        return subprocess.run(command,cwd=root,timeout=45,stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,check=False).returncode
    except (OSError,subprocess.SubprocessError) as error:
        logger.warning('assistant sync unavailable: %s',type(error).__name__)
        return None


def _valid(root,path):
    try:
        parts=path.relative_to(root).parts
        if path.is_symlink() or not path.is_file():return False
        cursor=root
        if cursor.is_symlink():return False
        for part in parts:
            cursor=cursor/part
            if cursor.is_symlink():return False
        if len(parts)==4 and parts[:2]==('SYSTEM','SEMANTIC_SEARCH') and parts[2] in {'requests','results'}:
            if path.suffix!='.json':return False
            read=semantic_search_jobs.get_request if parts[2]=='requests' else semantic_search_jobs.get_result
            return read(root,path.stem) is not None
        return parts==('SYSTEM','AI_USAGE','summary.json') and ai_usage.read_published(root) is not None
    except (ValueError,OSError):return False


def sync_paths(vault,paths,message='assistant: search and usage'):
    root=Path(vault).absolute();targets=list(dict.fromkeys(Path(x).absolute() for x in paths))
    if not targets or not all(_valid(root,p) for p in targets):return False
    with queue_store.queue_lock(root/'SYSTEM/assistant-sync'):
        if _git(['git','pull','--rebase','--autostash'],root)!=0:return False
        if not all(_valid(root,p) for p in targets):return False
        names=[str(p) for p in targets]
        if _git(['git','add','--',*names],root)!=0:return False
        changed=_git(['git','diff','--cached','--quiet','--',*names],root)
        if changed not in (0,1):return False
        if changed==1 and _git(['git','commit','-m',message,'--',*names],root)!=0:return False
        return _git(['git','push'],root)==0


def pull(vault):
    root=Path(vault).absolute()
    if not root.is_dir() or root.is_symlink():return False
    with queue_store.queue_lock(root/'SYSTEM/assistant-sync'):
        return _git(['git','pull','--rebase','--autostash'],root)==0
