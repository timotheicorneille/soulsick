# Soulsick avec Nix

Le flake à la racine fournit le paquet, un module NixOS et un shell de dev. Les dépendances Python viennent de `apps/api/uv.lock` (via uv2nix), celles du front de `bun.lock` : rien à maintenir en double.

## Lancer sans installer

```sh
nix run github:prop4n/soulsick
```

Les données vont dans `./data/` comme en dev. Les chemins se changent avec les variables `SOULSICK_CONFIG_DIR`, `SOULSICK_MUSIC_DIR`, `SOULSICK_DOWNLOADS_DIR`, `SOULSICK_PORT`.

## Module NixOS

```nix
{
  inputs.soulsick.url = "github:prop4n/soulsick";

  outputs = { nixpkgs, soulsick, ... }: {
    nixosConfigurations.serveur = nixpkgs.lib.nixosSystem {
      modules = [
        soulsick.nixosModules.default
        {
          services.soulsick = {
            enable = true;
            musicDir = "/srv/musique";   # doit exister, accessible en écriture par l'utilisateur soulsick
            openFirewall = true;         # 50300 et 50301 en TCP
          };
        }
      ];
    };
  };
}
```

L'interface écoute sur le port 5030. Le compte Soulseek, le port d'écoute Soulseek, Navidrome et Qobuz se règlent dans l'interface, comme avec Docker. Si tu changes le port d'écoute, aligne `services.soulsick.soulseekPorts` (le port, puis le port + 1).

Le service tourne sous un utilisateur `soulsick` sans privilèges. Pour que Navidrome lise la bibliothèque, mets les deux dans un groupe commun (`users.users.soulsick.extraGroups`).

## Développer

```sh
nix develop          # python 3.12, uv, bun
bun run dev
bun run test
```

## Mettre à jour les hashes du front

La dérivation des `node_modules` a un hash par système (`nix/web.nix`). Quand `bun.lock` change, `nix build .#web` échoue et donne le nouveau hash à recopier. Le commentaire en tête de `nix/web.nix` explique comment obtenir celui d'aarch64 depuis une machine x86_64.

## Vérifier

```sh
nix flake check      # build + test VM : le service démarre et sert l'interface
```
