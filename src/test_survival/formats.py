"""Canonical fixed result headers."""
COMMITS=['index','sha','date','subject','changes-src-main','changes-pom']
DEV=['class','method','kind']
LLM=['file','class','method','kind','body_hash','round','technique']
FIRST_FAILURES=['index','sha','group','id','class','method','kind','flaky']
LINES=['file','line','covered']


def survival(groups):return ['index','sha','date','subject','evaluated']+[g+s for g in groups for s in ['_alive','_lost']]


def changed(groups):
    fields=['index','sha','date','subject']
    for partition in ['all','scope','other']:
        fields.append(partition+'_executable')
        fields.extend(partition+'_'+g+s for g in groups for s in ['_covered','_percent'])
    fields.extend(g+s for g in groups for s in ['_lost','_pre_image_covered'])
    return fields
