#!/usr/bin/env bash
# Install Java 21 (Eclipse Temurin JRE) and the veraPDF validator into ./tools (Linux). Safe to re-run.
# Downloads: api.adoptium.net (~50 MB) and software.verapdf.org (~33 MB). Needs curl and python3.
set -euo pipefail
cd "$(dirname "$0")/.."
TOOLS="${PDFA11Y_TOOLS:-$PWD/tools}"
mkdir -p "$TOOLS/_dl"

case "$(uname -m)" in
  x86_64|amd64)  ARCH=x64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  *) echo "Unsupported CPU: $(uname -m)"; exit 1 ;;
esac

if ! ls "$TOOLS"/jre/*/bin/java >/dev/null 2>&1; then
  echo "Downloading Java 21 (Eclipse Temurin JRE) ..."
  curl -fsSL -o "$TOOLS/_dl/jre.tar.gz" "https://api.adoptium.net/v3/binary/latest/21/ga/linux/$ARCH/jre/hotspot/normal/eclipse"
  mkdir -p "$TOOLS/jre"
  tar -xzf "$TOOLS/_dl/jre.tar.gz" -C "$TOOLS/jre"
fi
JAVA="$(ls "$TOOLS"/jre/*/bin/java | head -1)"

if ! ls "$TOOLS"/verapdf/bin/cli-*.jar >/dev/null 2>&1; then
  echo "Downloading veraPDF ..."
  curl -fsSL -o "$TOOLS/_dl/verapdf-installer.zip" "https://software.verapdf.org/rel/verapdf-installer.zip"
  rm -rf "$TOOLS/_dl/vp"
  python3 -m zipfile -e "$TOOLS/_dl/verapdf-installer.zip" "$TOOLS/_dl/vp"
  JAR="$(ls "$TOOLS"/_dl/vp/*/verapdf-izpack-installer-*.jar | head -1)"
  cat > "$TOOLS/_dl/auto-install.xml" <<XML
<AutomatedInstallation langpack="eng">
  <com.izforge.izpack.panels.htmlhello.HTMLHelloPanel id="welcome"/>
  <com.izforge.izpack.panels.target.TargetPanel id="install_dir"><installpath>$TOOLS/verapdf</installpath></com.izforge.izpack.panels.target.TargetPanel>
  <com.izforge.izpack.panels.packs.PacksPanel id="sdk_pack_select">
    <pack index="0" name="veraPDF GUI" selected="true"/>
    <pack index="1" name="veraPDF Mac and *nix Scripts" selected="false"/>
    <pack index="2" name="veraPDF Validation model" selected="false"/>
    <pack index="3" name="veraPDF Documentation" selected="false"/>
    <pack index="4" name="veraPDF Sample Plugins" selected="false"/>
  </com.izforge.izpack.panels.packs.PacksPanel>
  <com.izforge.izpack.panels.install.InstallPanel id="install"/>
  <com.izforge.izpack.panels.finish.FinishPanel id="finish"/>
</AutomatedInstallation>
XML
  "$JAVA" -Djava.awt.headless=true -jar "$JAR" "$TOOLS/_dl/auto-install.xml"
fi

rm -rf "$TOOLS/_dl"
echo "Tools ready in $TOOLS"
ls "$TOOLS"/verapdf/bin/cli-*.jar >/dev/null && echo "  veraPDF: OK" || { echo "  veraPDF: NOT FOUND"; exit 1; }
