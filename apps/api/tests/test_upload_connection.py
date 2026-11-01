"""Envoi à un pair au port fermé : la connexion de fichier est choisie sur l'échange complet.

Cas réel (rubiksenjoyer, slskd, port fermé) : la connexion directe s'ouvrait sans mener nulle part,
aioslsk la gardait et fermait l'indirecte (la bonne), puis fermait aussi la connexion « second-chance »
que slskd ouvrait avec notre ticket. Le pair ne pouvait jamais rien télécharger.
"""

import asyncio
import itertools
from types import SimpleNamespace

from aioslsk.exceptions import ConnectionReadError, PeerConnectionError

from soulsick.slsk.engine import SafeTransferManager


class _Conn:
    def __init__(self, name, offset=0, fails=False, hangs=False):
        self.name, self.offset, self.fails, self.hangs = name, offset, fails, hangs
        self.sent: list[bytes] = []
        self.closed = False

    async def send_message(self, data):
        self.sent.append(bytes(data))

    async def receive_transfer_offset(self):
        if self.hangs:
            await asyncio.sleep(3600)
        if self.fails:
            raise ConnectionReadError("closed")
        return self.offset

    async def disconnect(self, reason):
        self.closed = True


class _Network:
    def __init__(self, direct, indirect):
        self.direct, self.indirect = direct, indirect
        self._ticket_generator = itertools.count(100)

    async def _make_direct_connection(self, ticket, username, typ):
        return await self._outcome(self.direct)

    async def _make_indirect_connection(self, ticket, username, typ):
        return await self._outcome(self.indirect)

    @staticmethod
    async def _outcome(o):
        delay, value = o
        await asyncio.sleep(delay)
        if isinstance(value, Exception):
            raise value
        return value


async def _race(direct, indirect, incoming=None, timeout=1.0):
    """incoming: (delay, connection) for the peer's own connection, or None."""
    fut = asyncio.get_running_loop().create_future()
    if incoming:
        asyncio.get_running_loop().call_later(incoming[0], fut.set_result, incoming[1])
    mgr = SimpleNamespace(_network=_Network(direct, indirect), FILE_CONNECTION_TIMEOUT=timeout)
    return await SafeTransferManager._file_connection(mgr, SimpleNamespace(username="rubiksenjoyer"), 7, fut)


async def test_une_directe_fantome_ne_fait_plus_tomber_l_indirecte():
    """Le cas réel : la directe s'ouvre (box, pare-feu) mais aucune position n'arrive jamais."""
    ghost, real = _Conn("ghost", hangs=True), _Conn("indirect", offset=0)
    conn, offset = await _race(direct=(0, ghost), indirect=(0.05, real))
    assert conn is real and offset == 0
    assert real.sent == [(7).to_bytes(4, "little")], "notre ticket envoyé sur l'indirecte"
    assert ghost.closed and not real.closed


async def test_la_connexion_second_chance_du_pair_est_acceptee():
    theirs = _Conn("theirs", offset=1234)
    conn, offset = await _race(direct=(0, PeerConnectionError("closed")), indirect=(0, PeerConnectionError("no answer")),
                               incoming=(0.05, theirs))
    assert conn is theirs and offset == 1234
    assert theirs.sent == [], "le pair a déjà envoyé le ticket sur sa connexion"


async def test_la_directe_sert_quand_elle_marche():
    direct = _Conn("direct", offset=0)
    conn, _ = await _race(direct=(0, direct), indirect=(0.5, _Conn("late")))
    assert conn is direct


async def test_une_position_refusee_laisse_la_place_aux_autres():
    conn, _ = await _race(direct=(0, _Conn("bad", fails=True)), indirect=(0.05, _Conn("ok")))
    assert conn.name == "ok"


async def test_rien_ne_vient():
    assert await _race(direct=(0, PeerConnectionError("x")), indirect=(0, PeerConnectionError("y")), timeout=0.2) is None


def test_alias_des_partages_stable_entre_redemarrages():
    """Cas réel : après un redéploiement, « @@gfipp » est devenu « @@kpncb » (adresse MAC du
    conteneur) et tous les téléchargements en attente chez les pairs ont reçu « File not shared. »."""
    from soulsick.slsk.engine import FilteringSharesManager

    def alias(salt, path="/music", offset=0):
        m = FilteringSharesManager.__new__(FilteringSharesManager)
        m.salt = salt
        return m.generate_alias(path, offset)

    a = alias(b"secret-1")
    assert a == alias(b"secret-1"), "même dossier, même installation : même alias"
    assert len(a) == 5 and a.isalpha() and a.islower()
    assert a != alias(b"secret-2"), "une autre installation a d'autres alias"
    assert a != alias(b"secret-1", "/shares")
    assert a != alias(b"secret-1", offset=1), "décalage en cas de collision"


def test_le_scan_des_partages_a_son_propre_pool(tmp_path):
    """Cas réel : au démarrage, la lecture des tags de 2 379 fichiers partagés (sshfs) occupait le
    pool de threads par défaut ; la notation d'une chasse restait bloquée derrière."""
    from soulsick.settings_store import SoulseekSettings
    from soulsick.slsk.engine import SlskEngine, _Client

    engine = SlskEngine(str(tmp_path))
    client = _Client(engine._build_settings(SoulseekSettings(username="u", password="p")))
    assert client.shares.executor_factory is not None
    pool = client.shares.executor_factory()
    assert pool._max_workers == 2
    pool.shutdown()


async def test_arreter_le_client_n_attend_pas_la_fin_du_scan_des_partages(tmp_path):
    """Cas réel : à chaque redémarrage du client (port du VPN connu, puis à chaque changement),
    l'arrêt de l'ancien attendait la fin de la lecture des tags de 3 745 fichiers sur sshfs,
    sur la boucle d'événements : plus rien ne répondait pendant 2 min 30."""
    import threading
    import time

    from soulsick.settings_store import SoulseekSettings
    from soulsick.slsk.engine import SlskEngine, _Client

    engine = SlskEngine(str(tmp_path))
    client = _Client(engine._build_settings(SoulseekSettings(username="u", password="p")))
    await client.shares.start()
    release = threading.Event()
    # Le scan en cours : un fichier qui n'en finit pas, et d'autres en attente derrière
    for _ in range(5):
        client.shares.executor.submit(release.wait, 10)
    try:
        t0 = time.perf_counter()
        await asyncio.wait_for(client.shares.stop(), timeout=2)
        assert time.perf_counter() - t0 < 1
    finally:
        release.set()
