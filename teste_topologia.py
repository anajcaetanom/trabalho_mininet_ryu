import sys
from functools import partial
from time import sleep

import networkx as nx
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController


def read_topo(edge_list_file):
    # Função do NetworX que cria grafos de acordo com o tipo de representação "Edge List"
    graph = nx.read_edgelist(
        edge_list_file, 
        comments='#', 
        nodetype=str,
        data=False
    )
    graph.remove_edges_from(list(nx.selfloop_edges(graph))) # Remove self-loops
    return graph

def start_network(edge_list_file):
    # Fazer o parsing do arquivo 'edge_list_file' para extrair as arestas
    topo = read_topo(edge_list_file)

    net = Mininet(controller=RemoteController, 
                  switch=partial(OVSKernelSwitch, protocols='OpenFlow13'))
    c0 = net.addController('c0', port=6633)
    
    # Adicionar switches e links usando net.addSwitch e net.addLink baseando-se no arquivo, e;
    # Adicionar um host para cada switch e conectá-los
    for switch in sorted(topo.nodes, key=lambda n: (len(n), n)):
        net.addSwitch(switch)
        host = 'h' + switch[1:]
        net.addHost(host)
        net.addLink(host, switch)

    # Adicionar links entre switches baseando-se no arquivo
    for a, b in topo.edges:
        net.addLink(a, b)   
    
    net.start()
    print("Aguardando inicialização do controlador...")
    sleep(5)  # tempo para o Ryu descobrir os enlaces via LLDP
    
    print("Enviando Gratuitous ARP de todos os hosts para descoberta pelo Ryu...")
    for h in net.hosts:
        h.cmd('arping -U -I ' + h.defaultIntf().name + ' -c 1 ' + h.IP())
        sleep(0.1)
    
    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    if len(sys.argv) > 1:
        start_network(sys.argv[1])
    else:
        print("Uso: sudo python teste_topologia.py <arquivo_topologia.txt>")