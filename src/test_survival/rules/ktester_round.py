"""Evidence required to accept an author-tool round."""


def fully_visited(run):
    post = run.get('post_process')
    if run.get('status') != 'completed' or not isinstance(post, dict) or post.get('status') != 'completed':
        return False
    return (all(type(post.get(key)) is int for key in ('visited', 'total', 'skipped'))
            and 0 <= post['skipped'] <= post['visited'] == post['total']
            and 'error' in post)
