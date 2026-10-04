from panel.modules.base import Port
from panel.netcheck import port_status

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
UDP = HEADER + "  1: 00000000:1E61 00000000:0000 07 00000000:00000000 00:00000000 00000000    99        0 1 2 0\n"
TCP = (HEADER
       + "  1: 00000000:01BB 00000000:0000 0A 00000000:00000000 00:00000000 00000000    99        0 1 2 0\n"
       + "  2: 0100007F:1E62 0100007F:C350 01 00000000:00000000 00:00000000 00000000    99        0 1 2 0\n")


def test_port_status_reads_proc_net(tmp_path):
    (tmp_path / "udp").write_text(UDP.replace("\n", "\n"))
    (tmp_path / "tcp").write_text(TCP.replace("\n", "\n"))
    rows = port_status([Port(7777, 7778, "udp"), Port(443, 443, "tcp+udp", required=False)], tmp_path)
    got = {(r["port"], r["proto"]): r["listening"] for r in rows}
    assert got == {(7777, "udp"): True, (7778, "udp"): False, (443, "tcp"): True, (443, "udp"): False}
    assert [r["required"] for r in rows] == [True, True, False, False]


def test_established_tcp_is_not_listening(tmp_path):
    (tmp_path / "tcp").write_text(TCP)
    rows = port_status([Port(7778, 7778, "tcp")], tmp_path)
    assert rows[0]["listening"] is False  # state 01 (established) on :1E62 is not a listener


def test_missing_proc_files_mean_nothing_listening(tmp_path):
    assert not any(r["listening"] for r in port_status([Port(1, 2, "udp")], tmp_path))
