"""Folder keys for generated-test evidence."""


def folder_keys(classes):
    """Map each target class to its folder key.

    The key is the simple class name, or the full name when two target classes
    share a simple name. Prompts still use the simple name.
    """
    simple = [c.rsplit('.', 1)[-1] for c in classes]
    return {c: (c if simple.count(s) > 1 else s) for c, s in zip(classes, simple)}
