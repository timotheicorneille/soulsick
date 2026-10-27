# services.soulsick : Soulsick en service systemd, sans conteneur.
#
# Seuls les chemins et l'adresse d'écoute web se déclarent ici. Le reste (compte
# Soulseek, port d'écoute Soulseek, Navidrome, Qobuz...) se règle dans l'interface
# et vit dans la base SQLite de stateDir.
{ config, lib, ... }:
let
  cfg = config.services.soulsick;
in
{
  options.services.soulsick = {
    enable = lib.mkEnableOption "Soulsick, client Soulseek avec gestion de bibliothèque";

    package = lib.mkOption {
      type = lib.types.package;
      description = "Paquet Soulsick (fourni par le flake).";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
      description = "Adresse d'écoute de l'interface web.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 5030;
      description = "Port de l'interface web.";
    };

    stateDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/soulsick";
      description = "Base SQLite, réglages, pochettes en cache (SOULSICK_CONFIG_DIR).";
    };

    musicDir = lib.mkOption {
      type = lib.types.path;
      example = "/srv/musique";
      description = ''
        Bibliothèque où Soulsick range la musique importée (SOULSICK_MUSIC_DIR).
        Elle doit exister et être accessible en écriture par user ou group : le
        module ne la crée pas, pour ne jamais changer les droits d'une bibliothèque
        existante.
      '';
    };

    downloadsDir = lib.mkOption {
      type = lib.types.path;
      default = "${cfg.stateDir}/downloads";
      defaultText = lib.literalExpression ''"''${config.services.soulsick.stateDir}/downloads"'';
      description = "Réception des téléchargements avant import (SOULSICK_DOWNLOADS_DIR).";
    };

    forwardedPortFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/gluetun/forwarded_port";
      description = ''
        Derrière un VPN à redirection de port : fichier contenant le port redirigé.
        Soulseek écoute alors sur ce port au lieu de celui des réglages.
      '';
    };

    soulseekPorts = lib.mkOption {
      type = lib.types.listOf lib.types.port;
      default = [ 50300 50301 ];
      description = ''
        Ports TCP Soulseek (écoute, puis obfuscation) ouverts par openFirewall.
        Ils doivent correspondre au port d'écoute choisi dans Réglages > Soulseek
        (le second est le port d'écoute + 1).
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Ouvre soulseekPorts dans le pare-feu (pas le port web).";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "soulsick";
      description = "Utilisateur du service.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "soulsick";
      description = "Groupe du service.";
    };

    extraReadWritePaths = lib.mkOption {
      type = lib.types.listOf lib.types.path;
      default = [ ];
      description = "Chemins en écriture en plus de stateDir, musicDir et downloadsDir.";
    };

    environment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = { SOULSICK_DISABLE_ENGINE = "1"; };
      description = "Variables d'environnement supplémentaires.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users = lib.mkIf (cfg.user == "soulsick") {
      soulsick = {
        isSystemUser = true;
        group = cfg.group;
        home = cfg.stateDir;
      };
    };
    users.groups = lib.mkIf (cfg.group == "soulsick") { soulsick = { }; };

    systemd.tmpfiles.settings."10-soulsick" = lib.genAttrs [ cfg.stateDir cfg.downloadsDir ] (_: {
      d = { inherit (cfg) user group; mode = "0750"; };
    });

    systemd.services.soulsick = {
      description = "Soulsick";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      environment = {
        SOULSICK_CONFIG_DIR = cfg.stateDir;
        SOULSICK_MUSIC_DIR = cfg.musicDir;
        SOULSICK_DOWNLOADS_DIR = cfg.downloadsDir;
        SOULSICK_HOST = cfg.host;
        SOULSICK_PORT = toString cfg.port;
      } // lib.optionalAttrs (cfg.forwardedPortFile != null) {
        SOULSICK_FORWARDED_PORT_FILE = toString cfg.forwardedPortFile;
      } // cfg.environment;

      serviceConfig = {
        ExecStart = lib.getExe cfg.package;
        User = cfg.user;
        Group = cfg.group;
        Restart = "on-failure";
        RestartSec = 5;
        # Dossiers rangés lisibles par le groupe (Navidrome par exemple)
        UMask = "0002";

        ReadWritePaths = [ cfg.stateDir cfg.musicDir cfg.downloadsDir ] ++ cfg.extraReadWritePaths;
        ProtectSystem = "strict";
        ProtectHome = "read-only";
        PrivateTmp = true;
        PrivateDevices = true;
        NoNewPrivileges = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" "AF_NETLINK" ];
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" "~@privileged" ];
        CapabilityBoundingSet = "";
      };
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall cfg.soulseekPorts;
  };
}
