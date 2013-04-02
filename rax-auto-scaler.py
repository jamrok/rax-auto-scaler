#!/usr/bin/env python

import os,sys,time,io
import subprocess, shutil
import re
import pyrax
import random
from datetime import datetime
import json
import paramiko
from getpass import getpass

"""
GLOBAL VARIABLES
"""
main_config_file = os.path.expanduser("~/.rax-auto-scaler.conf")
main_ssh_key_file = os.path.expanduser("~/.ssh/id_rsa")
main_ssh_pub_file = os.path.expanduser("~/.ssh/id_rsa.pub")
main_ssh_user = "root"
main_simulate_load_script="""#!/bin/bash
script_name=$(basename $0)
description="This program adds simulated load on the server. 
Syntax: $script_name [<loadavg>]
Simply run the script on it's own or pass in the load you want to add.
You can cancel the load simulations by running [ killall -v $script_name ]"

[ ! -z "$1" ] && loop=$1 || loop=2

getload(){ top -b -n1 | grep 'load average:' | sed -e 's/.*average: //'; }

load=$loop
echo "########################################################################"
echo "$description"
echo "########################################################################"
echo "Load Avg is: $(getload)"
echo "Increasing Load Avg by: $load"
echo "########################################################################"

while [ $loop -ge 0 ]; do
  let loop=$loop-1
  let count=$load-$loop
  [ $loop -ge 0 ] && {
    echo "\_Spawning child $count"
    while true; do : ; done &
  }
done
while true; do
  getload
  sleep 5
done
"""

def timestamp():
    return datetime.now().strftime('%Y-%m-%d_%H:%M:%S')

def log(*vals):
    msg = []
    for val in vals:
        msg.append(str(val))
    print timestamp(), "".join(msg)

def log_except(*val):
    log ("".join(val),"\nDetails: %s:\n%s" % (sys.exc_info()[0], sys.exc_info()[1]))

def readfile(file):
    try:
        with io.open(os.path.expanduser(file), "r") as fh:
            return fh.read()
    except:
        log_except("Unable to read file")
        exit()

def wait_for_status(obj, status="status", value="ACTIVE"
    , interval=30, attempts=2, verbose=False, callback = None, quit=False):
    try:
        pyrax.utils.wait_until(obj, status, value
            , interval=interval, attempts=attempts
            , callback = None, verbose=verbose)
    except:
        log_except("An error occured while waiting on status")
        if quit:
            sys.exit()

class ServerConfig(object):
    def __init__(self, username, apikey, name, server_info):
        self.username = username
        self.apikey = apikey
        self.name = name
        self.server_tag = server_info["server_tag"]
        self.clone_slave_tag = server_info["clone_slave_tag"]
        self.load_balancer = server_info["load_balancer"]
        self.min_servers = server_info["min_servers"]
        self.min_loadavg = server_info["min_loadavg"]
        self.max_loadavg = server_info["max_loadavg"]
        self.load_watch_time = server_info["load_watch_time"]
        self.interval = server_info["interval"]
        self.build_server_increment = server_info["build_server_increment"]
        self.drop_server_decrement = server_info["drop_server_decrement"]
        
class ConfigParser(object):
    config = {}
    server_list = []
    def __init__(self):
        self.get_server_config()
        for server_name in self.config["servers"]:
            self.server_list.append(ServerConfig(
                self.config["username"]
                , self.config["apikey"]
                , server_name
                , self.config["servers"][server_name]
            ))
                
    def get_configs(self):
        return self.server_list
    
    def get_server_config(self):
        pass
        log("Parsing Config")
        try:
            config_file = main_config_file
            with io.open(config_file, "r") as cfg:
                self.config = json.loads(cfg.read().lower())
            self.parse_config(self.config)
            log ("Parsed Config")
        except:
            log_except ("Unable to Parse Config!\n" \
                "Please check the file [ %s ]" % (main_config_file))
                #"\nDetails: %s:\n%s" % (sys.exc_info()[0], sys.exc_info()[1]))
            exit()
            
    def parse_config(self, config):
        main_attributes = ["username", "apikey", "servers"]
        servers_attributes = ["server_tag", "clone_slave_tag", "load_balancer"
            , "min_servers", "min_loadavg", "max_loadavg"
            , "load_watch_time", "interval", "build_server_increment"
            , "drop_server_decrement"]
            
        for attrib in main_attributes:
            if attrib not in config.keys():
                log_except ("Main Attribute missing from config file: ",attrib)
                raise Exception
            
        server_cfg = [ srv for srv in config["servers"] ]
        for server_name in server_cfg:
            if server_name in main_attributes + servers_attributes:
                log_except ("Description of server missing from config file")
                raise Exception
            
        for attrib in servers_attributes:
            for server_name in config["servers"]:
                if attrib not in config["servers"][server_name].keys():
                    raise Exception(
                        "Server Attribute missing from config file: "
                        , attrib)
                        

class RaxAutoScaler(object):
    cloud_servers = None
    cloud_lb = None
    _auto_scale_server_list = []
    _active_servers = []
    currentlb = None
    server_list = []
    ssh_keys = ""
    server_config = None
    
    @property
    def auto_scale_server_list(self):
        return self._auto_scale_server_list
    
    @auto_scale_server_list.setter
    def auto_scale_server_list(self, value):
        newlist = []
        current_list = self.cloud_servers.servers.list()
        for server in value:
            if server in [ s for s in current_list if 
            s.status in ("ACTIVE", "BUILD") ] and server not in newlist:
                #print "adding auto_scale_server_list", server
                newlist.append(server)
        newlist = sorted(newlist
                , key=lambda s: s.name.lower())
        self._auto_scale_server_list = newlist
    
    @property
    def active_servers(self):
        return self._active_servers
    
    @active_servers.setter
    def active_servers(self, value):
        newlist = []
        current_list = self.cloud_servers.servers.list()
        for server in value:
            if server in [ s for s in current_list 
                if s.status == "ACTIVE" ] and server not in newlist:
                #print "adding active_servers", (server.id, server.name)
                newlist.append(server)
        newlist = sorted(newlist
                , key=lambda s: s.name.lower())
        self._active_servers = newlist
        
    def __init__(self, server_config):
        self.server_config = server_config
        self.username = self.server_config.username
        self.apikey = self.server_config.apikey
        self.get_ssh_keys()
        self.authenticate()
        self.get_server_list()
        self.update_load_balancer()
        self.get_valid_image()
 
    def authenticate(self):
        log("Authenticating [RaxAutoScaler]")
        try:
            pyrax.set_credentials(self.username
                , self.apikey)
            self.cloud_servers = pyrax.cloudservers
            self.cloud_lb = pyrax.cloud_loadbalancers
            log ("Authenticated")
        except:
            log_except ("Unable to Authenticate!\n" \
                "Please check config file [ %s ]" % (main_config_file))
                #"\nDetails: %s:\n%s" % (sys.exc_info()[0], sys.exc_info()[1]))
            sys.exit()

    def get_ssh_keys(self):
        if os.path.exists(main_ssh_pub_file):
            log("Found SSH Keys")
            self.ssh_keys = readfile(main_ssh_pub_file)
        else:
            log("Generating SSH Keys")
            if os.path.exists(main_ssh_key_file):
                shutil.move(main_ssh_key_file, main_ssh_key_file+".bak")
                #subprocess.call(["echo","test"])
            try:
                subprocess.call \
                (["ssh-keygen", "-b", "2048", "-t", "rsa", "-N", ""
                , "-f", main_ssh_key_file])
                subprocess.call \
                (["ssh-add"])
                self.ssh_keys = readfile(main_ssh_pub_file)
            except:
                log_except("Unable to Generate SSH Keys")
                exit()

    def get_server_list(self):
        log("Refreshing server info...")
        servers = CloudServers(self.username, self.apikey)
        server_list = servers.cloud_servers.servers.list
        server_tag_list = []
        if type(self.server_config.server_tag) is not list:
            server_tag_list.append(self.server_config.server_tag)
        else:
            server_tag_list = self.server_config.server_tag
        new_auto_scale_server_list = []
        new_active_servers = []
        for server_tag in server_tag_list:
            new_auto_scale_server_list.extend(
                [ srvr for srvr in server_list() 
                if server_tag in srvr.name 
                    and "DELET" not in srvr.status.upper()
                    and "PENDIN" not in srvr.status.upper()
                    and "ERROR" not in srvr.status.upper()
                     ])
        self.auto_scale_server_list = new_auto_scale_server_list            
        for server in self.auto_scale_server_list:
            new_active_servers.extend([ srv for srv in server_list() 
            if srv.name == server.name and srv.status == "ACTIVE" ])
        self.active_servers = new_active_servers
        if self.auto_scale_server_list == []:
            #log("no servers to scale, make them")
            self.autoscale_servers()
        elif len(self.auto_scale_server_list) < self.server_config.min_servers:
            #log (
            #self.autoscale_servers(
            #    self.server_config.min_servers - len(self.auto_scale_server_list))
            log_except("The minimum servers do not exist." \
                "\nPlease update config file:", main_config_file)
            exit()
        else:
            log("AutoScaling Servers: "
                , [s.name for s in self.auto_scale_server_list] )
            log("Active Servers: ", [ s.name for s in self.active_servers] )
#        self.wait_for_server_status("ACTIVE"
#            , self.auto_scale_server_list, timeout = 600, interval=15)
        return self.auto_scale_server_list

    def autoscale_servers(self, num=0):
        servers = CloudServers(self.username, self.apikey)
        num_queued = len(self.auto_scale_server_list)
        min_servers = self.server_config.min_servers
        if num < 0: #-Delete extra servers
            extra_servers = [ srv for srv in self.active_servers 
                if "extra" in srv.metadata["autoscale-key"].lower()]
            log("Extra (Deletable) Servers"
                , [srvr.name for srvr in extra_servers])
            if len(extra_servers) > 0:
                server_list = [srvr.name for srvr in extra_servers]
                server_list.sort(reverse=True)
                expired_servers = []
                for n, server in enumerate(server_list):
                    if num == 0:
                        break
                    expired_servers.append([ srvr for srvr 
                        in extra_servers if srvr.name == server][0])
                    num += 1
                for expired in expired_servers:
                    self.drop_nodes(self.get_nodes(expired))
                    log ("Deleting Server: ", expired.name)
                    expired.delete()
                timeout = time.time() + 300
                missing_info = []
                while time.time() <= timeout:
                    found = [ e for e in expired_servers if \
                        e.id in [ s.id for s
                            in servers.cloud_servers.servers.list() ]]
                    if len(found) == 0:
                        break
                    time.sleep(10)
        else: #-Add servers
            if num == 0:
                num = min_servers
                num_queued = 0
            server_tag = self.server_config.server_tag
            if type(server_tag) is list:
                server_tag = self.server_config.server_tag[0]
            for count in range(num):
                #server = [ srv for srv in self.active_servers
                #    if srv.name == max(
                #        [srvr.name for srvr in self.active_servers])]
                servername = "%s-%02d" % ( server_tag \
                    , num_queued+count+1 )
                if len([ srv for srv in self.active_servers 
                    if srv.name == servername ]) > 0:
                        continue
                #print servername, self.active_servers
                extra = False
                
                if len(self.active_servers) == 0:
                    meta = { "autoscale-key": "autoscale-master" }
                elif len(self.active_servers) < min_servers:
                    meta = { "autoscale-key": "autoscale-slave" }
                else:
                    #- "Extra" servers can be deleted later
                    meta = { "autoscale-key": "autoscale-extra" }
                    extra = True
                user = main_ssh_user
                files = { os.path.expanduser("~%s/.ssh/authorized_keys" 
                    % (user)) : self.ssh_keys
                    , "/root/simulate_load.sh"
                        : main_simulate_load_script  }
                
                if extra:
                    image = self.get_valid_image()
                    servers.create_by_id(meta, files, servername
                    , image.id, servers.get_flavors(image.minRam).id)
                else:
                    #print "creating", servername, "centos", 512, meta, files
                    servers.create(meta, files \
                        , servername, "ubuntu", 512)
            servers.show_server_info()
        self.get_server_list()
        self.wait_for_server_status("ACTIVE"
            , self.auto_scale_server_list, timeout = 600, interval=15)
        self.update_load_balancer()
        
    def update_load_balancer(self,server_list=None):
        lb_list = self.cloud_lb.list()
        if not server_list:
            server_list = self.active_servers
            
        lbname = self.server_config.load_balancer
        
        if lbname not in [ lb.name for lb in lb_list ]:
            # create load balancer:
            vip = self.cloud_lb.VirtualIP(type="PUBLIC")
            log("Creating Load Balancer: %s [%s]" % (lbname, vip))
            nodes = self.make_nodes(server_list)
            log("Nodes: ",nodes)
            self.currentlb = self.cloud_lb.create(lbname, port=80, protocol="HTTP",
                nodes=nodes, virtual_ips=[vip])
            wait_for_status(self.currentlb, "status", "ACTIVE"
            , interval=5, attempts=24, verbose=False)
        else:
            try:
                self.currentlb = [ lb for lb in lb_list if lbname in lb.name ][0]
                log("Checking Load Balancer: %s [%s]" % (self.currentlb.name
                    , self.currentlb.virtual_ips[0].address))
                #ensure all nodes are present
                missing_nodes = [ srv for srv in server_list 
                    if self.get_ip(srv, "private")
                        not in [ n.address for n in self.currentlb.nodes]]
                extra_nodes = [ node for node in self.currentlb.nodes
                    if node.address
                        not in [ self.get_ip(srv, "private") for srv in server_list]]
                if len(missing_nodes) > 0:
                    log("Adding new nodes to Load Balancer: ",lbname)
                    log("Nodes:", missing_nodes)
                    self.currentlb.add_nodes(self.make_nodes(missing_nodes))
                    wait_for_status(self.currentlb, "status", "ACTIVE"
                        , interval=5, attempts=24, verbose=False)
                if len(extra_nodes) > 0:
                    wait_for_status(self.currentlb, "status", "ACTIVE"
                        , interval=5, attempts=24, verbose=False)
                    #log("Removing orphaned nodes from Load Balancer: ",lbname)
                    #log("Nodes:", extra_nodes)
                    self.drop_nodes (extra_nodes)
                log("Load Balancer is: ", self.currentlb.status)
            except:
                log_except("An error occurred with the load balancer.")

    def get_nodes(self, server_list):
        if type(server_list) is not list:
            server_list = [ server_list ]
        if self.currentlb is not None:
            self.currentlb.get()
            nodes = [ node for node in self.currentlb.nodes
                    if node.address
                    in [ self.get_ip(srv, "private") for srv in server_list]]
        return nodes
                        
    def drop_nodes(self, extra_nodes):
        if self.currentlb is not None:
            if type(extra_nodes) is not list:
                extra_nodes = [ extra_nodes ]
            log("Removing the following nodes from Load Balancer:")
            log(extra_nodes)
            for node in extra_nodes:
                node.delete()
                wait_for_status(self.currentlb, "status", "ACTIVE"
                    , interval=5, attempts=24, verbose=False)
                            
    def make_nodes(self, server_list=None):
        nodes = []
        if server_list is not None and type(server_list) is not list:
            server_list = [ server_list ]
        if not server_list:
            server_list = self.auto_scale_server_list
        for srv in server_list:
            nodes.append(self.cloud_lb.Node(
                address=self.get_ip(srv, "private")
                , port=80, condition="ENABLED"))
        return nodes
    
    def wait_for_server_status(self, status, server_list
        , timeout=60, interval=15, verbose=True):
        self.get_server_list()
        log("Waiting up to %s seconds for all servers to be %s" % \
            (str(timeout), str(status)))
        for server in server_list:
            print "Server:",server.name
            wait_for_status(server, "status", (status, "ERROR")
                    , callback = None, interval=interval
                    , attempts=(timeout/interval if timeout/interval > 0 else 1)
                , verbose = verbose)
        if type(status) is not list:
            status = [status]
        self.active_servers = [srvr for srvr in server_list 
            if not srvr.get() and
                srvr.status.upper() in status ]
#                srvr.status.upper() in [ srv.upper() for srv in status ]]
        if len(self.active_servers) == len(server_list):
            log ("All Servers are %s %s" % 
                (status[0].upper(), str(self.active_servers)))
        else:
            wrong_status = [ srv for srv in server_list 
                if srv.id not in [ srvr.id for srvr in self.active_servers ]]
            log_except ("Some Servers are NOT %s: " % (status)
                , str(wrong_status)
                ,"\nAll Servers: ",str(server_list))
            #exit()
        self.get_server_list()

    def get_valid_image(self):
        image_tag = self.server_config.clone_slave_tag
        img_list = self.cloud_servers.images.list()
        valid_servers = [srv for srv in self.active_servers
                    if image_tag in srv.name]
        valid_image = [ img for img in img_list
            if hasattr(img, "server")
                and img.status in ("ACTIVE", "SAVING")
                and img.server["id"]
                in [ srv.id for srv in self.active_servers] ]
                #in [ srv.id for srv in valid_servers] ]
        if len(valid_image) > 0:
            valid_image =  [img for img in valid_image
                if img.created == max([ i.created for i in valid_image]) 
                    and img.status in ("ACTIVE", "SAVING") ][0]
            log ("Found a valid image: ", valid_image.name)
        #else:
        #    log("No Valid Images Found. ", valid_servers)
            #valid_servers = ""

        #- Use the last server that was created in the group
        if type(valid_image) in (list, str) and len(valid_image) == 0:
            if len(valid_servers) == 0:
                valid_servers = [ srv for srv in self.active_servers 
                    if srv.created
                        == max([ s.created for s in self.active_servers 
                                if "extra" not in s.metadata["autoscale-key"]])
                        and srv.status == "ACTIVE"
                    ][0]
            else:
                valid_servers = valid_servers[0]
            log ("Unable to find a image that matched config file" \
                ", creating image from server:", valid_servers)
            image = valid_servers.create_image(image_tag+"-base")
            valid_image = [ i for i in self.cloud_servers.images.list() 
                if i.id == image ][0]
        #print type(valid_image), valid_image
        wait_for_status(valid_image, "status", ("ACTIVE", "DELETED"), interval=15
            , attempts=36, verbose=True)
        return valid_image
    
    def monitor(self):
        #while True:
        log("Monitoring Servers")
        watching_for_addition = False
        watching_for_deletion = False
        timeout = time.time()
        while True:
            self.get_server_list()
            extra_servers = [ srv for srv in self.active_servers 
                if "extra" in srv.metadata["autoscale-key"].lower()]
            missing_info = []
            load = []
            for server in self.active_servers:
                load.append(self.get_loadavg(server)[0])
            
            #maxload = [avg for avg 
            #    in load if avg > self.server_config.max_loadavg]
            maxload = round(sum(load)/len(load)
                if len(load) > 0 else sum(load),2)
            #if len(maxload) > 0:
                
            if maxload > self.server_config.max_loadavg:
                timeout = time.time() + self.server_config.load_watch_time
                if watching_for_addition:
                    watching_for_addition = False
                    log ("Building additional server(s) due to high load: " 
                        , maxload)
                    self.autoscale_servers(
                        self.server_config.build_server_increment)
                else:
                    watching_for_addition = True
            elif maxload < self.server_config.min_loadavg \
                and len(extra_servers) > 0 \
                and len(self.active_servers) > self.server_config.min_servers:
                timeout = time.time() + self.server_config.load_watch_time
                if watching_for_deletion:
                    watching_for_deletion = False
                    log ("Removing extra server(s) due to reduced load: "
                        , maxload)
                    self.autoscale_servers(
                        -self.server_config.drop_server_decrement)
                else:
                    watching_for_deletion = True
            else:
                if watching_for_addition: watching_for_addition = False
                if watching_for_deletion: watching_for_deletion = False
            log("Load average across all servers: [%s]" % (maxload))
            time.sleep(self.server_config.interval)
            while time.time() < timeout:
                if watching_for_addition == True:
                    log ("High Load Detected [%s]! Watching for %s seconds." % 
                        (maxload, self.server_config.load_watch_time))
                elif watching_for_deletion == True:
                    log ("Reduced Load Detected [%s]! Watching for %s seconds." % 
                        (maxload, self.server_config.load_watch_time))                
                time.sleep(self.server_config.interval)
            
            
        #for ip in get_ip("public", self.active_servers):
        
    
    def get_ip(self, server, type="public"):
        #- Get IPv4 address since it's not always the first element
        networks = server.networks[type.lower()]
        return [ net for net in networks if "." in net or ":" not in net ][0]

    def get_loadavg(self, server):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        key = paramiko.RSAKey.from_private_key_file(main_ssh_key_file)
        stdin, stdout, stderr, load = None, None, None, [0]
        network = self.get_ip(server)
        log ("Connecting to %s (%s)" % (server.name, network))
        connected = False
        try:
            try:
                ssh.connect(network, username=main_ssh_user, pkey=key)
            except paramiko.AuthenticationException:
                for attempts in range(3):
                    try:
                        passwd = getpass("Password for %s@%s:" 
                            % (main_ssh_user, server.name))
                        #print passwd
                        ssh.connect(network, username=main_ssh_user
                            , password=passwd)
                        ssh.exec_command ("mkdir -p ~/.ssh;"+
                        "echo '%s' >> ~/.ssh/authorized_keys" % (self.ssh_keys))
                        connected = True
                        break
                    except paramiko.AuthenticationException:
                        continue
                raise paramiko.AuthenticationException
            stdin, stdout, stderr = \
            ssh.exec_command (
                "top -b -n1|grep 'load average:'|sed -e 's/.*average: //'"
                )
        except:
            if not connected:
                log_except("Unable to connect to server: ", server.name
                    , "\nEnsure that the ssh-keys are configured on that server.")
                time.sleep(5)
                #sys.exit()
        if stdout is not None:
            load = [ float(avg) for avg 
                in stdout.readline().strip("\n").split(",") ]
            ssh.close()
        log("Load Average: ", load)
        return load

class CloudServers(object):
    cloud_servers = None
    image_list = None
    flavor_list = None
    myservers = []

    class MyServer(object):
            name = None
            image = None
            flavor = None    
            def __init__(self, *args):
                if args:
                    self.name = args[0]
                    self.image = args[1]
                    self.flavor = args[2]

    def __init__(self, username, apikey):
        self.username = username
        self.apikey = apikey
        self.authenticate()

    def authenticate(self):
        #log("Authenticating: [CloudServers]")
        try:
            pyrax.set_credentials(self.username, self.apikey)
            self.cloud_servers = pyrax.cloudservers
            #log ("Authenticated")
        except:
            log ("Unable to Authenticate [CloudServers]!\n")
            sys.exit()

    def get_images(self, name = None, refresh = False):
        #log("Getting Server Image(s)")
        """get server image(s) based on the name"""
        if self.image_list == None or refresh:
            self.image_list = sorted(self.cloud_servers.images.list()
                , key=lambda img: img.name.lower(), reverse=True)

        images = self.image_list
        if name:
            try:
                images = [image for image in images 
                    if re.match(str(name).lower(),image.name.lower())]
            except:
                images = None
        #log("Retrieved Images")
        return images

    def get_flavors(self, ram = None, refresh = False):
        #log("Getting Flavor(s)")
        """get a server flavor based on the size"""
        if self.flavor_list == None or refresh:
            self.flavor_list = self.cloud_servers.flavors.list()

        flavors = self.flavor_list
        if ram:
            try:
                flavors = [flavor for flavor in flavors 
                    if flavor.ram == ram or flavor.ram/1024 == ram][0]
            except:
                flavors = None
        #log("Retrieved Flavors")
        return flavors

    def create_by_id(self, meta=None, files=None, *server_info):
        # expects {str}name, {str}image, {int}flavor or a MyServer object
        list_of_servers = []
        if server_info and type(server_info[0]) is not self.MyServer:
            list_of_servers.append(self.MyServer(*server_info))
        elif not server_info:
            #create 3 random webservers
            # TODO: Put back to 3
            for count in range(3):
                list_of_servers.append(
                    self.MyServer(
                    "web%02d" % (count+1)
                    , random.choice(self.get_images("centos|ubuntu"))
                    , self.get_flavors(512)
                    ))

        for srvr in list_of_servers:
            if type(srvr.image) in (str,unicode):
                log("Creating ('{}', '{}', {})".format(srvr.name, srvr.image
                    , srvr.flavor))
                self.myservers.append(self.cloud_servers.servers.create(srvr.name
                    , srvr.image, srvr.flavor, meta=meta, files=files))
            else:
                print "Creating Server Name: {}, OS: {}, RAM: {}MB".format(
                    srvr.name, srvr.image.name, srvr.flavor.ram)
                #print "create('{}', '{}', {})\n".format(srvr.name, srvr.image.id
                #    , srvr.flavor.id)
                self.myservers.append(self.cloud_servers.servers.create(srvr.name
                    , srvr.image.id, srvr.flavor.id, meta=meta, files=files))
            if files is not None:
                for file in files:
                    log("Injecting file: %s" % (file))
    

    def create(self, meta=None, files=None, *server_info):
        #accept {str}name, {str}image_name_regex, {int}num
        if not server_info:
            self.create_by_id()
        else:
            self.create_by_id(meta, files, server_info[0]
                , self.get_images(server_info[1])[0]
                , self.get_flavors(server_info[2]))

    def show_server_info(self, server_list = []):
        #accept a {list}server_ids or {list}Server objects
        new_server_list = []
        if not server_list:
            server_list = self.myservers
        
        if not server_list:
            log("No server list provided")
            sys.exit()
        
        #get initial update of server info
        try:
            if type(server_list[0]) in (str, unicode) \
                    and type(server_list) is not list:
                log("Obtaining Info for ServerID:", server_list)
                new_server_list.append(
                    self.cloud_servers.servers.get(server_list))
            else:
                for server in server_list:
                    if type(server) in (str, unicode):
                        print "Obtaining Info for ServerID:", server
                        new_server_list.append(
                            self.cloud_servers.servers.get(server))
                    else:
                        print "Obtaining Info for Server Name: {}, ID: {}"\
                            .format(server.name, server.id)
                        new_server_list.append(
                            self.cloud_servers.servers.get(server.id))
        except:
            log ("Server not found!\n",
                "\nDetails: {}:\n{}"
                    .format(sys.exc_info()[0], sys.exc_info()[1]))
            #sys.exit()  
            
        timeout = time.time() + 300
        timed_out = True
        missing_info = []
        while time.time() <= timeout:
            for missing_info in [server for server in new_server_list if 
                    len(server.networks) == 0
                    and server.status != "ERROR" ]:
                log("Generating network info for [%s], please wait..." \
                    % (missing_info.name))
                missing_info.get() # Refresh server info
            
            missing_info = [server for server in new_server_list if 
                    len(server.networks) == 0 and server.status != "ERROR" ]                                
            if len(missing_info) == 0:
                timed_out = False
                break;
            time.sleep(30)

        print "{line}\nServer Info:\n{line}".format(line = "#" * 80)
        for server in new_server_list:
            orig_server_info = [ srv for srv in self.myservers 
                if srv.id == server.id ]
            orig_server_info = orig_server_info[0] \
                if len(orig_server_info) > 0 else None
            print "\nName: {}\nRoot Password: {}" \
                "\nPublic IPv4: {}\nPublic IPv6: {}\nPrivate IPv4: {}".format(
                server.name
                , orig_server_info.adminPass
                    if hasattr(orig_server_info, "adminPass")
                    else "Unavailable"
                , [ n for n in server.networks["public"] if "." in n ][0]
                    if len(server.networks) != 0 else "Not yet available."
                , [ n for n in server.networks["public"] if "." not in n ][0]
                    if len(server.networks) != 0 else "Not yet available."
                , server.networks["private"][0]
                    if len(server.networks) != 0 else "Not yet available."
            )
        if timed_out:
            log("Unable to retrieve complete network information. Please " \
                "check online at [ https://mycloud.rackspace.com ]")
        else:
            log("Server(s) Created! Please check online at " \
                "[ https://mycloud.rackspace.com ]")
        
        #set each update the network info for each server
        #self.myservers = new_server_list
        #print self.myservers 
        #print new_server_list

scale = []
for server_config in ConfigParser().get_configs():
    try:
        scaler = RaxAutoScaler(server_config)
        scaler.monitor()
        scale.append(scaler)
    except (KeyboardInterrupt, SystemExit):
        log_except("An fatal error has occurred.")
        sys.exit()
    #except:
    #    log_except("An error has occurred. Trying to continue.")
    #    continue

#srvr_list = scale.cloud_servers.servers.list()
