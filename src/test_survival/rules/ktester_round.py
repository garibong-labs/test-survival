"""Evidence required to accept an author-tool round."""
import json


def fully_visited(run):
    post = run.get('post_process')
    if run.get('status') != 'completed' or not isinstance(post, dict) or post.get('status') != 'completed':
        return False
    return (all(type(post.get(key)) is int for key in ('visited', 'total', 'skipped'))
            and 0 <= post['skipped'] <= post['visited'] == post['total']
            and 'error' in post)


def reusable_round(directory, run):
    if not fully_visited(run):
        return False
    receipt = directory/'test-classpath.json'
    if not receipt.is_file():
        return True
    classpath = json.loads(receipt.read_text())
    added = classpath.get('added', [])
    if ('replaced' in classpath or not any(jar.startswith('mockito-core-') for jar in added)
            or any(jar.startswith('byte-buddy-') and not jar.startswith('byte-buddy-agent-') for jar in added)):
        return True
    # An omitted Byte Buddy could also have been compatible. Require evidence
    # of Byte Buddy failing during Mockito initialization before superseding a paid round.
    log = directory/'generation.log'
    if log.is_file():
        with log.open(errors='replace') as lines:
            buddy_failure = False
            for line in lines:
                if (('NoSuchFieldError' in line and 'JAVA_V21' in line)
                        or 'Failed to resolve the class file version of the current VM: Unknown Java version' in line):
                    buddy_failure = True
                elif buddy_failure and 'org.mockito.internal.PremainAttachAccess.getInstrumentation' in line:
                    return False
                elif 'net.bytebuddy.ClassFileVersion' not in line:
                    buddy_failure = False
    return True
