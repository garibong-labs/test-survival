"""Compile an offline fixture plugin against the runner's own Maven APIs."""
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

source, repository, scratch = map(Path, sys.argv[1:])
scratch.mkdir(parents=True, exist_ok=True)
maven = Path(shutil.which('mvn')).resolve().parents[1]
subprocess.run(['javac','-cp',str(maven/'lib/*'),'-d',str(scratch),str(source/'DependencyMojo.java')],check=True)
mojos=[]
for goal in ('go-offline','build-classpath','list','observe'):
    mojos.append(f'''<mojo><goal>{goal}</goal><requiresProject>true</requiresProject>
      <implementation>fixture.DependencyMojo</implementation><language>java</language>
      <instantiationStrategy>per-lookup</instantiationStrategy><executionStrategy>once-per-session</executionStrategy>
      <threadSafe>true</threadSafe><parameters>
      <parameter><name>project</name><type>org.apache.maven.project.MavenProject</type><required>true</required><editable>false</editable></parameter>
      <parameter><name>session</name><type>org.apache.maven.execution.MavenSession</type><required>true</required><editable>false</editable></parameter>
      <parameter><name>goal</name><type>java.lang.String</type><required>true</required><editable>false</editable></parameter>
      </parameters><configuration>
      <project implementation="org.apache.maven.project.MavenProject" default-value="${{project}}"/>
      <session implementation="org.apache.maven.execution.MavenSession" default-value="${{session}}"/>
      <goal implementation="java.lang.String" default-value="{goal}"/>
      </configuration></mojo>''')
plugin = repository/'org/apache/maven/plugins/maven-dependency-plugin/0-fixture'
plugin.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(plugin/'maven-dependency-plugin-0-fixture.jar','w') as jar:
    for path in scratch.rglob('*.class'):jar.write(path,str(path.relative_to(scratch)))
    jar.writestr('META-INF/maven/plugin.xml',f'''<plugin><name>Offline effective-model fixture</name>
      <groupId>org.apache.maven.plugins</groupId><artifactId>maven-dependency-plugin</artifactId><version>0-fixture</version>
      <goalPrefix>dependency</goalPrefix><isolatedRealm>false</isolatedRealm><inheritedByDefault>true</inheritedByDefault>
      <mojos>{''.join(mojos)}</mojos><dependencies/></plugin>''')
(plugin/'maven-dependency-plugin-0-fixture.pom').write_text('<project><modelVersion>4.0.0</modelVersion><groupId>org.apache.maven.plugins</groupId><artifactId>maven-dependency-plugin</artifactId><version>0-fixture</version><packaging>maven-plugin</packaging></project>')
for version in ('1','2','3'):
    directory=repository/f'fixture/library/{version}';directory.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(directory/f'library-{version}.jar','w') as jar:jar.writestr('fixture-version',version)
    (directory/f'library-{version}.pom').write_text(f'<project><modelVersion>4.0.0</modelVersion><groupId>fixture</groupId><artifactId>library</artifactId><version>{version}</version></project>')
