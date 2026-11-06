"""Parent-model cache eligibility, without Maven's repository-resolution semantics."""
from test_survival.adapters.context_inputs import collect


def model(artifact, parent='', profiles=''):
    return (f'<project><modelVersion>4.0.0</modelVersion>{parent}<groupId>fixture</groupId>'
            f'<artifactId>{artifact}</artifactId><version>1</version><packaging>pom</packaging>{profiles}</project>')


def parent(artifact, relative):
    return (f'<parent><groupId>fixture</groupId><artifactId>{artifact}</artifactId><version>1</version>'
            f'<relativePath>{relative}</relativePath></parent>')


PROFILE='<profiles><profile><id>marker</id><activation><file><exists>${basedir}/activate.marker</exists></file></activation></profile></profiles>'


def test_transitive_local_parent_activation_uses_each_child(tmp_path):
    for name in ('parent','middle','a','b'):(tmp_path/name).mkdir()
    (tmp_path/'parent/pom.xml').write_text(model('parent',profiles=PROFILE))
    (tmp_path/'middle/pom.xml').write_text(model('middle',parent('parent','../parent/pom.xml')))
    for name in ('a','b'):(tmp_path/name/'pom.xml').write_text(model(name,parent('middle','../middle/pom.xml')))
    before=collect(tmp_path,tmp_path/'a')
    assert collect(tmp_path,tmp_path/'a')==before
    (tmp_path/'a/activate.marker').touch()
    active=collect(tmp_path,tmp_path/'a')
    assert active!=before and not active['activation']['unbounded']
    (tmp_path/'a/activate.marker').unlink()
    assert collect(tmp_path,tmp_path/'a')==before


def test_repository_parent_fallbacks_disable_reuse(tmp_path):
    (tmp_path/'pom.xml').write_text(model('child',parent('parent','')))
    for relative in ('','missing.xml','mismatch.xml'):
        (tmp_path/'mismatch.xml').write_text(model('wrong'))
        (tmp_path/'pom.xml').write_text(model('child',parent('parent',relative)))
        first=collect(tmp_path);second=collect(tmp_path)
        assert first['activation']['unbounded']
        assert first!=second


def test_unprovable_local_parent_coordinates_disable_reuse(tmp_path):
    (tmp_path/'parent.xml').write_text(model('parent').replace('<version>1</version>','<version>${revision}</version>'))
    (tmp_path/'pom.xml').write_text(model('child',parent('parent','parent.xml')))
    assert collect(tmp_path)!=collect(tmp_path)


def test_aggregation_is_not_a_parent_cycle(tmp_path):
    (tmp_path/'child').mkdir()
    (tmp_path/'pom.xml').write_text(model('parent',profiles='<modules><module>child</module></modules>'))
    (tmp_path/'child/pom.xml').write_text(model('child',parent('parent','../pom.xml')))
    assert not collect(tmp_path,tmp_path/'child')['activation']['unbounded']
    assert collect(tmp_path,tmp_path/'child')==collect(tmp_path,tmp_path/'child')
