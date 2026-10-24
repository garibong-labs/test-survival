from pathlib import Path
from test_survival.adapters.java_worker import parse_reports,method_state


def test_launcher_names_parameterized_and_error_precedence(tmp_path):
    (tmp_path/'TEST-junit.xml').write_text('''<testsuite><testcase classname="p.T" name="display name"><system-out>[test-template:works(java.lang.String)]</system-out></testcase><testcase classname="p.T" name="works(String)[2]"><failure type="org.opentest4j.AssertionFailedError" message="expected"/></testcase><testcase classname="p.T" name="skip()"><skipped/></testcase></testsuite>''')
    obs=parse_reports(tmp_path)
    assert method_state({'class':'p.T','method':'works'},obs,{'timeout':False})==('fail','expected')
    assert method_state({'class':'p.T','method':'skip'},obs,{'timeout':False})[0]=='absent'
    assert method_state({'class':'p.T','method':'missing'},obs,{'timeout':False})[0]=='absent'
    assert method_state({'class':'p.T','method':'works'},obs,{'timeout':True})[0]=='timeout'
