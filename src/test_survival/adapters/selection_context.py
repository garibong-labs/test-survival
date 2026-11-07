"""Resolved third-party entries actually placed on the test JVM classpath."""
import json
from pathlib import Path
import re


def collect(w, job):
    folder, cp = w.classpath(job['index'])
    work = w.EXP/job['work']; work.mkdir(parents=True, exist_ok=True)
    listing = work/'dependencies.txt'
    listing.unlink(missing_ok=True)
    context = dict(version=1, sha=job['sha'], known=False, entries=[])
    try:
        w.maven_dependencies.maven('dependency:list', folder, work/'resolve', w.TIMEOUTS['build'], w.execute,
            options=('-DincludeScope=test', '-DoutputAbsoluteArtifactFilename=true', '-DoutputFile='+str(listing)),
            module=getattr(w, 'MODULE', '.'))
        coordinates = {}
        for line in listing.read_text().splitlines():
            line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
            line = re.sub(r'\s+\(optional\)(?=\s|$)', '', line)
            match = re.match(r'^\s*([^\s]+?):(/.*?)(?:\s+--.*)?\s*$', line)
            if not match: continue
            parts = match[1].split(':')
            if len(parts) not in (5,6): continue
            group, artifact, kind = parts[:3]
            classifier, version = ('', parts[3]) if len(parts) == 5 else (parts[3], parts[4])
            coordinates[match[2]] = [group, artifact, version, classifier, kind]
        manifest = json.loads((w.HELPERS/'classpath-artifacts.json').read_text())
        for item in manifest:
            group, artifact, version = item['coordinate'].split(':')
            coordinates[str(w.EXP/'work/libs'/item['filename'])] = [group, artifact, version, item.get('classifier',''), 'jar']
        version = w.jacoco_version(folder/'target/classes')
        paths = cp.split(':') + [str(w.EXP/f'work/libs/org.jacoco.cli-{version}-nodeps.jar'),
                                str(w.EXP/f'work/libs/org.jacoco.agent-{version}-runtime.jar')]
        entries = []
        for path in paths:
            if not path or Path(path) == folder/'target/classes': continue
            if path not in coordinates: raise ValueError('Unresolved classpath artifact: '+path)
            entries.append(coordinates[path])
        context.update(known=True, entries=entries)
    except (ValueError, OSError, RuntimeError) as exc:
        context['reason'] = str(exc)
    w.write_json(w.EXP/job['output'], context)
