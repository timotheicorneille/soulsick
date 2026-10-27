{
  description = "Soulsick, client Soulseek self-hosté avec gestion de bibliothèque intégrée";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # Les dépendances Python viennent de apps/api/uv.lock, pas de nixpkgs
    # (aioslsk n'y est pas, et les versions y sont souvent en retard)
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, pyproject-nix, uv2nix, pyproject-build-systems }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        web = pkgs.callPackage ./nix/web.nix { };
        soulsick = pkgs.callPackage ./nix/package.nix {
          inherit web pyproject-nix uv2nix pyproject-build-systems;
        };
        default = soulsick;
      });

      nixosModules.default = { pkgs, ... }: {
        imports = [ ./nix/module.nix ];
        services.soulsick.package = nixpkgs.lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      };

      checks = forAllSystems (pkgs: {
        vm = pkgs.testers.runNixOSTest (import ./nix/test.nix { inherit self; });
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.python312 pkgs.uv pkgs.bun ];
          env = {
            UV_PYTHON_DOWNLOADS = "never";
            # Les wheels manylinux installées par uv (greenlet...) cherchent libstdc++ hors du store
            LD_LIBRARY_PATH = nixpkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];
          };
        };
      });
    };
}
