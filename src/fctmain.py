import argparse
import os
import subprocess
import time
from functools import partial

import networkx as nx
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController


def build_torus_3x3():
    """Gera a topologia Torus 3x3 (s1..s9) via NetworkX."""
    g = nx.Graph()
    nodes = [f"s{i}" for i in range(1, 10)]
    g.add_nodes_from(nodes)

    for r in range(3):
        for c in range(3):
            curr = f"s{r * 3 + c + 1}"
            right = f"s{r * 3 + ((c + 1) % 3) + 1}"
            down = f"s{((r + 1) % 3) * 3 + c + 1}"
            g.add_edge(curr, right)
            g.add_edge(curr, down)

    return g, nodes


def build_spine_leaf():
    """Gera a topologia Spine-Leaf (s1..s2 Spine, s3..s8 Leaf) via NetworkX."""
    g = nx.Graph()
    spines = ["s1", "s2"]
    leaves = [f"s{i}" for i in range(3, 9)]
    g.add_nodes_from(spines + leaves)

    for leaf in leaves:
        for spine in spines:
            g.add_edge(leaf, spine)

    return g, leaves


def disable_ipv6(net):
    """Desabilita IPv6 em todos os hosts e switches."""
    for node in net.hosts + net.switches:
        node.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null")
        node.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null")
        node.cmd("sysctl -w net.ipv6.conf.lo.disable_ipv6=1 >/dev/null")


def start_ryu(routing_method, log_fp):
    """Inicia o controlador Ryu com o metodo de roteamento selecionado."""
    env = os.environ.copy()
    env["ROUTING_METHOD"] = routing_method
    return subprocess.Popen(
        ["ryu-manager", "--observe-links", "topo_discover.py"],
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
    )


def run_experiment(topology_name, routing_method, exp_num, block_size="25M", link_bw=10):
    os.makedirs("resultados", exist_ok=True)
    prefix = f"resultados/{topology_name}_{routing_method}_exp{exp_num}"

    with open(f"{prefix}_ryu.log", "w", encoding="utf-8") as ryu_log, \
         open(f"{prefix}_bwm.csv", "w", encoding="utf-8") as bwm_log:

        # 1. Inicia o controlador Ryu com OSPF ou ECMP
        print(f"[1/6] Iniciando controlador Ryu ({routing_method.upper()})...")
        ryu_proc = start_ryu(routing_method, ryu_log)
        time.sleep(3)

        # 2. Monta a topologia no Mininet
        if topology_name == "torus":
            graph, host_switches = build_torus_3x3()
        else:
            graph, host_switches = build_spine_leaf()

        net = Mininet(
            controller=RemoteController,
            switch=partial(OVSKernelSwitch, protocols="OpenFlow13"),
            link=TCLink,
        )
        net.addController("c0", ip="127.0.0.1", port=6633)

        for sw in sorted(graph.nodes, key=lambda n: (len(n), n)):
            net.addSwitch(sw)

        for sw in host_switches:
            host = "h" + sw[1:]
            net.addHost(host)
            net.addLink(host, sw, bw=link_bw)

        for a, b in graph.edges:
            net.addLink(a, b, bw=link_bw)

        net.start()
        disable_ipv6(net)
        bwm_proc = None

        try:
            # 3. Descoberta de topologia e hosts
            print("[2/6] Aguardando descoberta LLDP e executando pingAll()...")
            time.sleep(5)
            net.pingAll()
            time.sleep(2)

            # 4. Inicia monitoramento passivo com bwm-ng em background
            print("[3/6] Iniciando bwm-ng em background...")
            bwm_proc = subprocess.Popen(
                ["bwm-ng", "-o", "csv", "-c", "0", "-t", "500", "-u", "bits"],
                stdout=bwm_log,
                stderr=subprocess.DEVNULL,
            )

            # 5. Inicia servidores Iperf em todos os hosts
            print(f"[4/6] Iniciando servidores e clientes Iperf todos-para-todos (bloco={block_size})...")
            for h in net.hosts:
                h.cmd("iperf -s -D")
            time.sleep(1)

            client_procs = []
            t_start = time.time()
            for src in net.hosts:
                for dst in net.hosts:
                    if src != dst:
                        p = src.popen(f"iperf -c {dst.IP()} -n {block_size} -y C")
                        client_procs.append(p)

            # 6. Execucao condicional do Experimento 1 ou 2
            if exp_num == 1:
                print("[5/6] Experimento 1 (FCT Classico): aguardando conclusao dos fluxos...")
            else:
                wait_fail = 15
                print(f"[5/6] Experimento 2 (Falha): aguardando {wait_fail}s para derrubar enlace...")
                time.sleep(wait_fail)
                if topology_name == "torus":
                    print("  -> Derrubando enlace s4 <-> s5")
                    net.configLinkStatus("s4", "s5", "down")
                else:
                    print("  -> Derrubando enlace s3 <-> s1")
                    net.configLinkStatus("s3", "s1", "down")

            for p in client_procs:
                p.wait()

            fct_total = time.time() - t_start
            print(f"\n>>> FCT Total ({topology_name} | {routing_method} | exp {exp_num}): {fct_total:.3f} s\n")

            with open(f"{prefix}_fct.txt", "w", encoding="utf-8") as f:
                f.write(
                    f"topology={topology_name}\n"
                    f"routing={routing_method}\n"
                    f"exp={exp_num}\n"
                    f"fct_seconds={fct_total:.4f}\n"
                )

        finally:
            # 7. Finalizacao limpa
            print("[6/6] Encerrando processos residuais e parando Mininet...")
            if bwm_proc is not None:
                bwm_proc.terminate()
                bwm_proc.wait()
            subprocess.run(["pkill", "-9", "iperf"], check=False, stderr=subprocess.DEVNULL)
            ryu_proc.terminate()
            ryu_proc.wait()
            net.stop()


if __name__ == "__main__":
    setLogLevel("warning")
    parser = argparse.ArgumentParser(description="Automacao de Experimentos SDN - Parte 2")
    parser.add_argument("--topology", required=True, choices=["torus", "spineleaf"])
    parser.add_argument("--routing", required=True, choices=["ospf", "ecmp"])
    parser.add_argument("--exp", required=True, type=int, choices=[1, 2])
    parser.add_argument("--size", default="25M", help="Tamanho do bloco Iperf por fluxo (ex: 25M, 50M)")
    parser.add_argument("--bw", default=10, type=int, help="Banda dos enlaces em Mbps")
    args = parser.parse_args()

    run_experiment(args.topology, args.routing, args.exp, block_size=args.size, link_bw=args.bw)