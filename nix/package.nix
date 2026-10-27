# API Python + front compilé. Les dépendances Python sont celles de apps/api/uv.lock.
{ lib, python312, callPackage, makeWrapper, runCommand, web, pyproject-nix, uv2nix, pyproject-build-systems }:
let
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ../apps/api; };

  # Les wheels quand il y en a : pas de compilation de rapidfuzz, pydantic-core, uvloop...
  overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

  pythonSet = (callPackage pyproject-nix.build.packages { python = python312; }).overrideScope (
    lib.composeManyExtensions [
      pyproject-build-systems.overlays.wheel
      overlay
    ]
  );

  venv = pythonSet.mkVirtualEnv "soulsick-env" workspace.deps.default;
in
runCommand "soulsick-${pythonSet.soulsick.version}" {
  nativeBuildInputs = [ makeWrapper ];
  passthru = { inherit venv web; };
  meta = {
    description = "Client Soulseek self-hosté avec gestion de bibliothèque intégrée";
    homepage = "https://github.com/prop4n/soulsick";
    mainProgram = "soulsick";
    platforms = lib.platforms.linux;
  };
} ''
  mkdir -p $out/bin
  makeWrapper ${venv}/bin/soulsick $out/bin/soulsick \
    --set-default SOULSICK_WEB_DIST ${web}
''
