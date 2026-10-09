import sys
from functools import partial
from time import sleep

import networkx as nx
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController


def read_topo(topo_file):
    graph = nx.read_edgelist(
        topo_file, 
        comments='#', 
        nodetype=str,
        data=False
    )

    graph.remove_

def start_network(edge_list_file):
    net = Mininet(controller=RemoteController, switch=OVSKernelSwitch)
    c0 = net.addController('c0', port=6633)
    
    # TODO: Fazer o parsing do arquivo 'edge_list_file' para extrair as arestas
    # TODO: Adicionar switches e links usando net.addSwitch e net.addLink baseando-se no arquivo
    # TODO: Adicionar um host para cada switch e conectá-los
    
    net.start()
    print("Aguardando inicialização do controlador...")
    sleep(3)
    
    print("Enviando Gratuitous ARP de todos os hosts para descoberta pelo Ryu...")
    for h in net.hosts:
        h.cmd('arping -U -c 1 ' + h.IP())
        sleep(0.1)
    
    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    if len(sys.argv) > 1:
        start_network(sys.argv[1])
    else:
        print("Uso: sudo python teste_topologia.py <arquivo_topologia.txt>")