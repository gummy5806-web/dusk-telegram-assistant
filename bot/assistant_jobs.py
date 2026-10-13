"""Mac-side semantic jobs and aggregate AI usage, sharing the existing worker."""
from pathlib import Path
import logging
import subprocess
import ai_usage
import llm_embeddings
import queue_sync
import semantic_recall
import semantic_search_jobs as jobs
logger=logging.getLogger(__name__)


def _search(vault,query):
    return semantic_recall.search(vault,query,embed=llm_embeddings.embed)


def _dirty(vault):
    roots=[str(Path(vault)/jobs.QUEUE_ROOT/'results'),str(Path(vault)/ai_usage.SUMMARY_REL)]
    try:
        result=subprocess.run(['git','status','--porcelain','--untracked-files=all','--',*roots],
                              cwd=vault,timeout=20,capture_output=True,text=True,check=False)
        paths=[]
        if result.returncode==0:
            for line in (result.stdout or '').splitlines():
                name=line[3:].strip()
                if name and not name.startswith('"'):
                    path=Path(vault)/name
                    if queue_sync._valid(Path(vault).absolute(),path.absolute()):paths.append(path)
        ahead=subprocess.run(['git','log','-1','--format=%H','@{upstream}..HEAD','--',*roots],
                             cwd=vault,timeout=20,capture_output=True,text=True,check=False)
        if ahead.returncode==0 and (ahead.stdout or '').strip():
            summary=Path(vault)/ai_usage.SUMMARY_REL
            if summary.exists():paths.append(summary)
            results=[jobs.result_path(vault,x['id']) for x in jobs.requests(vault) if jobs.get_result(vault,x['id'])]
            if results:paths.append(results[0])
        return paths
    except (OSError,subprocess.SubprocessError) as error:
        logger.warning('assistant outbox check failed: %s',type(error).__name__)
        return []


def run(vault,search=None,embed=None):
    root=Path(vault).absolute();paths=[]
    try:
        before=ai_usage.read_published(root)
        identifier=jobs.process_one(root,search=search or _search)
        if identifier:paths.append(jobs.result_path(root,identifier))
        indexed=semantic_recall.index(root,embed=embed or llm_embeddings.embed)
        if indexed.get('error'):
            logger.warning('semantic indexing deferred')
        published=ai_usage.publish(root)
        after=ai_usage.read_published(root)
        if before!=after:paths.append(published)
        paths.extend(_dirty(root))
        paths=list(dict.fromkeys(paths))
        return queue_sync.sync_paths(root,paths) if paths else True
    except (OSError,ValueError) as error:
        logger.warning('assistant work deferred: %s',type(error).__name__)
        return False
