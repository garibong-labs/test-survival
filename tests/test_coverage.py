def test_changed_line_insert_delete_and_module_coordinates():
    from test_survival.stages.cover import changed_lines
    pre,post=changed_lines('--- a/src/main/java/A.java\n+++ b/src/main/java/A.java\n@@ -2,0 +3,2 @@\n+a\n+b\n@@ -8,1 +10,0 @@\n-old\n')
    assert pre=={'src/main/java/A.java':[8]}
    assert post=={'src/main/java/A.java':[3,4]}
    assert changed_lines('--- a/module/src/main/java/A.java\n+++ b/module/src/main/java/A.java\n@@ -1 +1 @@\n', './module') == ({'src/main/java/A.java':[1]}, {'src/main/java/A.java':[1]})
