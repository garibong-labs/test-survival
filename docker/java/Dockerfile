ARG JDK_VERSION=17
FROM eclipse-temurin:${JDK_VERSION}-jdk
ARG MAVEN_VERSION=3.9.9
RUN apt-get update && apt-get install -y --no-install-recommends python3 git curl ca-certificates \
    && curl -fsSL https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz | tar -xz -C /opt \
    && ln -s /opt/apache-maven-${MAVEN_VERSION}/bin/mvn /usr/local/bin/mvn \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /experiment
