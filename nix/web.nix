# Front Vue compilé (apps/web/dist), servi par l'API via SOULSICK_WEB_DIST.
{ lib, stdenvNoCC, bun, autoPatchelfHook, stdenv }:
let
  version = (lib.importJSON ../apps/web/package.json).version;

  # Dépendances JS téléchargées par bun : dérivation à sortie fixe, seule autorisée
  # à aller sur le réseau. Les paquets natifs (rolldown, tailwind, lightningcss)
  # dépendent de la plateforme, d'où un hash par système. --cpu/--os les fixent
  # explicitement : les deux hashes se recalculent depuis n'importe quelle machine.
  # Quand bun.lock change : remplacer les hashes par lib.fakeHash, puis
  # `nix build .#web` (et `--system aarch64-linux` si possible), et recopier ceux
  # que donne l'erreur. Sans machine aarch64, le même `bun install --cpu=arm64
  # --os=linux` suivi de `nix hash path --sri` sur le dossier recopié donne le sien.
  bunCpu = {
    x86_64-linux = "x64";
    aarch64-linux = "arm64";
  }.${stdenvNoCC.hostPlatform.system};

  nodeModules = stdenvNoCC.mkDerivation {
    pname = "soulsick-web-node-modules";
    inherit version;

    src = lib.fileset.toSource {
      root = ../.;
      fileset = lib.fileset.unions [
        ../package.json
        ../bun.lock
        ../apps/web/package.json
      ];
    };

    nativeBuildInputs = [ bun ];

    buildPhase = ''
      runHook preBuild
      export HOME=$TMPDIR
      bun install --frozen-lockfile --ignore-scripts --no-progress --cpu=${bunCpu} --os=linux
      runHook postBuild
    '';

    installPhase = ''
      runHook preInstall
      mkdir -p $out
      cp -R node_modules $out/
      if [ -d apps/web/node_modules ]; then
        mkdir -p $out/apps/web
        cp -R apps/web/node_modules $out/apps/web/
      fi
      runHook postInstall
    '';

    dontFixup = true;
    impureEnvVars = lib.fetchers.proxyImpureEnvVars;
    outputHashMode = "recursive";
    outputHashAlgo = "sha256";
    outputHash = {
      x86_64-linux = "sha256-/FavIhugY2gU3K1qqkqpyKIyj26MhFnkz8SrLznwizM=";
      aarch64-linux = "sha256-lRMYd0VxHH/lf/GHcxnzgTjGAdn1ZBCCTz2GnEW1O6U=";
    }.${stdenvNoCC.hostPlatform.system};
  };
in
stdenvNoCC.mkDerivation {
  pname = "soulsick-web";
  inherit version;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../package.json
      ../bun.lock
      ../apps/web
    ];
  };

  # Les modules natifs .node sont liés pour une glibc FHS
  nativeBuildInputs = [ bun autoPatchelfHook ];
  buildInputs = [ stdenv.cc.cc.lib ];
  # bun installe aussi les variantes musl, jamais chargées sur une glibc
  autoPatchelfIgnoreMissingDeps = [ "libc.musl-*.so.1" ];

  configurePhase = ''
    runHook preConfigure
    cp -R ${nodeModules}/. .
    chmod -R u+w node_modules apps/web
    autoPatchelf node_modules
    patchShebangs node_modules
    runHook postConfigure
  '';

  # Comme le Dockerfile : pas de vue-tsc ici, le typecheck se fait en dev/CI
  buildPhase = ''
    runHook preBuild
    export HOME=$TMPDIR
    cd apps/web
    bun --bun node_modules/.bin/vite build
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    cp -R dist $out
    runHook postInstall
  '';

  passthru = { inherit nodeModules; };
}
