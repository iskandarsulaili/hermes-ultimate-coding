#!/usr/bin/env bash
# hermes-rea — bootstrap the caller-supplied REA analysis engines (all free; no paid tools).
#
#   bash tools/rea-install-engines.sh            # install everything missing
#   bash tools/rea-install-engines.sh --check    # report readiness only, change nothing
#
# Engines installed: Ghidra 12.1.4 (native decompiler), JADX headless MCP jar 0.7.1
# (APK decompilation), Rizin (offline native provider), apktool (APK resources),
# tshark + capinfos (pcap/packet captures), binwalk + unblob (firmware), sigrok-cli
# (logic recordings), WABT (WASM), pwntools/mitmproxy/frida-tools (pipx), wakaru (JS
# recovery, npm), gdb, .NET SDK + ilspycmd (managed decompilation).
#
# Deliberately NOT installed (paid / closed): Hopper, IDA, JEB. Those stay caller-supplied.
#
# Idempotent: every step skips what is already present and reports one status line.
set -uo pipefail
TOOLS="${REA_TOOLS_DIR:-$HOME/tools}"
LOCALBIN="${REA_LOCALBIN:-$HOME/.local/bin}"
PROVIDERS="$TOOLS/rea-providers"
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1
mkdir -p "$TOOLS" "$LOCALBIN"
export PATH="$LOCALBIN:$TOOLS/jadx-1.5.6/bin:$PATH"

note() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
want() {  # want <binary> <label>
  if have "$1"; then note "$(printf '%-14s OK    %s' "$2" "$(command -v "$1")")"; return 0; fi
  note "$(printf '%-14s MISS  %s' "$2" "$3")"; return 1
}

if [[ $CHECK_ONLY -eq 1 ]]; then
  note "== REA engine readiness (check only) =="
  want adb adb ""
  want objdump objdump ""
  have rizin && note "$(printf '%-14s OK    %s' rizin "$(command -v rizin)")" || note "$(printf '%-14s MISS  %s' rizin 'install Rizin')"
  [[ -n "${GHIDRA_INSTALL_DIR:-}" && -x "${GHIDRA_INSTALL_DIR:-}/support/analyzeHeadless" ]] \
    && note "$(printf '%-14s OK    %s' ghidra "$GHIDRA_INSTALL_DIR")" \
    || note "$(printf '%-14s MISS  %s' ghidra 'set GHIDRA_INSTALL_DIR to a Ghidra 12.1.x tree')"
  [[ -n "${REA_JADX_MCP_JAR:-}" && -f "${REA_JADX_MCP_JAR:-}" ]] \
    && note "$(printf '%-14s OK    %s' jadx-mcp "$REA_JADX_MCP_JAR")" \
    || note "$(printf '%-14s MISS  %s' jadx-mcp 'set REA_JADX_MCP_JAR to jadx-headless-mcp-0.7.1-all.jar')"
  want java java ""; want apktool apktool ""; want tshark tshark ""; want capinfos capinfos ""
  want binwalk binwalk ""; want unblob unblob ""; want sigrok-cli sigrok ""; want wasm2wat wabt ""
  want pwn pwntools ""; want mitmdump mitmproxy ""; want frida frida ""; want gdb gdb ""
  want wakaru wakaru ""; want dotnet dotnet ""; want ilspycmd ilspycmd ""
  wait 2>/dev/null || true
  exit 0
fi

JADX_JAR_SHA=6e5eacf500b64292bfb73c49797c1958f6ee44646e43e868039ae7feb573ff75

note "== apt engines =="
if have apt-get; then
  echo "wireshark-common wireshark-common/install-setuid boolean false" | sudo debconf-set-selections 2>/dev/null || true
  sudo apt-get install -y -qq --no-install-recommends \
    tshark wireshark-common binwalk apktool gdb sigrok-cli wabt unzip cabextract pipx >/dev/null 2>&1 || true
fi
want tshark tshark "apt install tshark"
want capinfos capinfos "apt install tshark"
want binwalk binwalk "apt install binwalk"
want apktool apktool "apt install apktool"
want gdb gdb "apt install gdb"
want sigrok-cli sigrok "apt install sigrok-cli"
want wasm2wat wabt "apt install wabt"

note "== Ghidra 12.1.4 (native decompilation) =="
GHIDRA_DIR="$TOOLS/ghidra_12.1.4_PUBLIC"
if [[ -x "$GHIDRA_DIR/support/analyzeHeadless" ]]; then
  note "ghidra         OK    $GHIDRA_DIR"
else
  ZIP="$TOOLS/ghidra_12.1.4_PUBLIC_20260921.zip"
  [[ -s "$ZIP" ]] || curl -sSL --fail --retry 3 -o "$ZIP" \
    https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.4_build/ghidra_12.1.4_PUBLIC_20260921.zip
  ( cd "$TOOLS" && unzip -q -o "$ZIP" ) && note "ghidra         OK    $GHIDRA_DIR" || note "ghidra         FAIL  download/extract"
fi

note "== JADX headless MCP jar 0.7.1 (APK decompilation provider) =="
JADX_JAR="$PROVIDERS/jadx-headless-mcp-0.7.1-all.jar"
mkdir -p "$PROVIDERS"
if [[ -s "$JADX_JAR" ]]; then
  note "jadx-mcp       OK    $JADX_JAR"
else
  if curl -sSL --fail --retry 3 -o "$JADX_JAR" \
      https://github.com/1013503897/jadx-headless-mcp/releases/download/v0.7.1/jadx-headless-mcp-0.7.1-all.jar \
     && echo "$JADX_JAR_SHA  $JADX_JAR" | sha256sum -c - >/dev/null 2>&1; then
    note "jadx-mcp       OK    $JADX_JAR (sha256 verified)"
  else
    note "jadx-mcp       FAIL  download/sha mismatch — REA android method decompilation stays unavailable"
  fi
fi
# the plain jadx CLI is a separate, optional convenience
if [[ ! -x "$TOOLS/jadx-1.5.6/bin/jadx" ]]; then
  ZIP="$TOOLS/jadx-1.5.6.zip"
  [[ -s "$ZIP" ]] || curl -sSL --fail --retry 3 -o "$ZIP" \
    https://github.com/skylot/jadx/releases/download/v1.5.6/jadx-1.5.6.zip
  ( cd "$TOOLS" && unzip -q -o "$ZIP" -d "$TOOLS/jadx-1.5.6" ) && chmod +x "$TOOLS/jadx-1.5.6"/bin/* 2>/dev/null
fi
[[ -x "$TOOLS/jadx-1.5.6/bin/jadx" ]] && note "jadx-cli       OK    $TOOLS/jadx-1.5.6/bin/jadx"

note "== Rizin 0.9.1 static (offline native provider) =="
RIZIN_DIR="$TOOLS/rizin-v0.9.1-static"
if have rizin; then
  note "rizin          OK    $(command -v rizin)"
else
  TAR="$TOOLS/rizin-v0.9.1-static-x86_64.tar.xz"
  [[ -s "$TAR" ]] || curl -sSL --fail --retry 3 -o "$TAR" \
    https://github.com/rizinorg/rizin/releases/download/v0.9.1/rizin-v0.9.1-static-x86_64.tar.xz
  rm -rf "$RIZIN_DIR"; mkdir -p "$RIZIN_DIR"
  # NOTE: no --strip-components here — the archive root already IS bin/, lib/, share/.
  tar -xJf "$TAR" -C "$RIZIN_DIR" >/dev/null 2>&1
  for b in rizin rz-bin rz-asm rz-ax rz-diff rz-hash; do
    [[ -f "$RIZIN_DIR/bin/$b" ]] && ln -sf "$RIZIN_DIR/bin/$b" "$LOCALBIN/$b"
  done
  have rizin && note "rizin          OK    $RIZIN_DIR/bin/rizin" || note "rizin          FAIL  extract"
fi

note "== pipx engines =="
if ! have pipx; then
  note "pipx           MISS  apt install pipx"
else
  for spec in "pwntools:pwn" "mitmproxy:mitmdump" "frida-tools:frida" "unblob:unblob"; do
    pkg="${spec%%:*}"; bin="${spec##*:}"
    if have "$bin"; then note "$(printf '%-14s OK    %s' "$bin" "$(command -v "$bin")")"; continue; fi
    pipx install --force "$pkg" >/dev/null 2>&1
    have "$bin" && note "$(printf '%-14s OK    %s' "$bin" "$(command -v "$bin")")" \
                || note "$(printf '%-14s FAIL  %s' "$bin" "pipx install $pkg (see ~/.local/state/pipx)")"
  done
fi

note "== wakaru (JS source recovery, npm) =="
if have wakaru; then
  note "wakaru         OK    $(command -v wakaru)"
else
  NPMBIN="$(dirname "$(command -v npm 2>/dev/null || echo /usr/bin/npm)")"
  "$NPMBIN/npm" install -g wakaru >/dev/null 2>&1
  [[ -x "$NPMBIN/wakaru" ]] && ln -sf "$NPMBIN/wakaru" "$LOCALBIN/wakaru"
  have wakaru && note "wakaru         OK    $(command -v wakaru)" || note "wakaru         FAIL  npm install -g wakaru"
fi

note "== .NET SDK + ilspycmd (managed decompilation) =="
if have dotnet; then
  note "dotnet         OK    $(command -v dotnet)"
else
  curl -sSL --fail --retry 2 -o "$TOOLS/dotnet-install.sh" https://dot.net/v1/dotnet-install.sh
  bash "$TOOLS/dotnet-install.sh" --channel 8.0 --install-dir "$TOOLS/dotnet" >/dev/null 2>&1
  [[ -x "$TOOLS/dotnet/dotnet" ]] && ln -sf "$TOOLS/dotnet/dotnet" "$LOCALBIN/dotnet"
  have dotnet && note "dotnet         OK    $TOOLS/dotnet" || note "dotnet         FAIL  install"
fi
if have dotnet && ! have ilspycmd; then
  DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 dotnet tool install -g ilspycmd >/dev/null 2>&1
  [[ -x "$HOME/.dotnet/tools/ilspycmd" ]] && ln -sf "$HOME/.dotnet/tools/ilspycmd" "$LOCALBIN/ilspycmd"
fi
have ilspycmd && note "ilspycmd       OK    $(command -v ilspycmd)" || note "ilspycmd       MISS  dotnet tool install -g ilspycmd"

note "== required by REA itself =="
want node node "install Node 22.19+/24.11+/26+"
want java java "install a JDK 17+ and set JAVA_HOME (Ghidra + JADX need it)"
want adb adb "install Android platform-tools"
want objdump objdump "apt install binutils"

echo
echo "Engine environment for REA (also written by the plugin to <hermes state>/rea/engine_env.json):"
echo "  export GHIDRA_INSTALL_DIR=$GHIDRA_DIR"
echo "  export JAVA_HOME=${JAVA_HOME:-$TOOLS/jdk21}"
echo "  export REA_JADX_MCP_JAR=$JADX_JAR"
echo "  export PATH=$LOCALBIN:$TOOLS/jadx-1.5.6/bin:\$PATH"
