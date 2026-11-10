"""Export approved trial files to a backend folder or a GitHub branch."""
import base64
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_GITHUB_REPOSITORY = 'LonghornNeurotech/EMG-Data-Base'


class Destination(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    kind: str = 'local'
    folder: str = Field(default='', max_length=500)
    repository: str = Field(default=DEFAULT_GITHUB_REPOSITORY, max_length=200)
    branch: str = Field(default='main', max_length=200)
    path: str = Field(default='recordings', max_length=300)

    @model_validator(mode='after')
    def validate_destination(self):
        if self.kind not in ('local', 'github'):
            raise ValueError('Destination must be local or github.')
        if self.kind == 'github':
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.repository):
                raise ValueError('GitHub repository must be owner/repository.')
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', self.branch) or '..' in self.branch or '//' in self.branch or self.branch.endswith(('/', '.', '.lock')):
                raise ValueError('Enter a valid existing GitHub branch.')
            parts = self.path.split('/') if self.path else []
            if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part) or part in ('.', '..') for part in parts):
                raise ValueError('GitHub folder must be a relative path without spaces or ..')
        return self


def export_local(destination, staging: Path, files: dict[str, bytes]):
    target = Path(destination.folder).expanduser().resolve() if destination.folder else (staging / 'approved').resolve()
    target.mkdir(parents=True, exist_ok=True)
    # CSVs precede the metadata file: a partial export never creates an accepted
    # sidecar before all samples are present. Retry accepts only identical files.
    for filename, content in files.items():
        path = target / filename
        try:
            with path.open('xb') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError(f'Existing destination file differs: {filename}')
    return {'kind': 'local', 'folder': str(target), 'files': list(files)}


def gh_call(payload):
    env = dict(os.environ, GH_PROMPT_DISABLED='1')
    try:
        result = subprocess.run(['gh', 'api', 'graphql', '--input', '-'], input=json.dumps(payload),
                                text=True, capture_output=True, timeout=45, env=env, check=False)
    except FileNotFoundError as exc:
        raise ValueError('GitHub CLI (gh) is not installed on the backend computer.') from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError('GitHub request timed out. Check the branch before retrying.') from exc
    if result.returncode:
        raise ValueError('GitHub export failed. Check gh authentication, repository permissions, branch and network.')
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError('GitHub returned an unreadable response.') from exc
    if data.get('errors'):
        raise ValueError('GitHub rejected the export. Check repository permissions, branch and network.')
    return data['data']


def export_github(destination, staging: Path, files: dict[str, bytes]):
    owner, repo = destination.repository.split('/')
    query = 'query($owner:String!,$repo:String!,$ref:String!){repository(owner:$owner,name:$repo){ref(qualifiedName:$ref){target{oid}}}}'
    data = gh_call({'query':query,'variables':{'owner':owner,'repo':repo,'ref':'refs/heads/'+destination.branch}})
    repository = data.get('repository')
    ref = repository.get('ref') if repository else None
    if not ref:
        raise ValueError('GitHub repository or existing branch not found or inaccessible.')
    head = ref['target']['oid']
    prefix = PurePosixPath(destination.path) if destination.path else PurePosixPath()
    expressions = {f'e{i}': f'{head}:{prefix / name}' for i, name in enumerate(files)}
    definitions = ','.join(f'${key}:String!' for key in expressions)
    fields = ' '.join(f'{key}:object(expression:${key}){{oid}}' for key in expressions)
    check_query = f'query($owner:String!,$repo:String!,{definitions}){{repository(owner:$owner,name:$repo){{{fields}}}}}'
    existing = gh_call({'query':check_query, 'variables':{'owner':owner, 'repo':repo, **expressions}}).get('repository')
    if not existing or any(existing.get(key) is not None for key in expressions):
        raise ValueError('A file with this trial ID already exists on GitHub; no files were overwritten.')
    additions = [{'path':str(prefix / name),'contents':base64.b64encode(content).decode('ascii')} for name,content in files.items()]
    mutation = '''mutation($input:CreateCommitOnBranchInput!){createCommitOnBranch(input:$input){commit{oid url}}}'''
    result = gh_call({'query':mutation,'variables':{'input':{
        'branch':{'repositoryNameWithOwner':destination.repository,'branchName':destination.branch},
        'expectedHeadOid':head,'message':{'headline':'Add approved sensor trial'},
        'fileChanges':{'additions':additions}}}})
    commit = result['createCommitOnBranch']['commit']
    return {'kind':'github','repository':destination.repository,'branch':destination.branch,
            'path':destination.path,'commit':commit['oid'],'url':commit['url'],'files':list(files)}


def export(destination, staging: Path, files: dict[str, bytes]):
    if destination.kind == 'local':
        return export_local(destination, staging, files)
    return export_github(destination, staging, files)
