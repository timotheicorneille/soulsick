# Test VM : le service démarre, l'API répond et sert le front.
# Pas de réseau dans le bac à sable, d'où le moteur Soulseek coupé.
{ self }:
{
  name = "soulsick";

  nodes.machine = { ... }: {
    imports = [ self.nixosModules.default ];

    systemd.tmpfiles.rules = [ "d /srv/musique 0750 soulsick soulsick -" ];

    services.soulsick = {
      enable = true;
      musicDir = "/srv/musique";
      openFirewall = true;
      environment.SOULSICK_DISABLE_ENGINE = "1";
    };
  };

  testScript = ''
    machine.wait_for_unit("soulsick.service")
    machine.wait_for_open_port(5030)

    state = machine.succeed("curl -sf http://127.0.0.1:5030/api/auth/state")
    assert '"setup_required":true' in state, state

    machine.succeed("curl -sf http://127.0.0.1:5030/ | grep -q '<title>Soulsick</title>'")
    machine.succeed("test -f /var/lib/soulsick/soulsick.db")
    machine.succeed("iptables -S nixos-fw | grep -q 'dport 50300'")
  '';
}
