################################################################################
RAX-AUTO-SCALER
################################################################################
Installation:
################################################################################
Simply download, install the packages listed below, create and edit the config file and run the program.

################################################################################
Summary:
################################################################################
This program can be configured to scale existing servers or simply to test it's ability to scale by providing fake server information in the configuration file. The servers specified in the config file will be created and/or monitored and will be automatically added and removed from the specified load balancer according to the thresholds in the config file.

A script [ /root/simulate_load.sh ] has also been provided on each remote server that is created (or connected to, if given a password). This script can be used to simulate an increase in server load.

################################################################################
Requirements:
################################################################################
- Config file: [ $HOME/.rax-auto-scaler.conf ]. Sample config provided.
- python-pip: apt-get install python-pip OR yum install python-pip
- Python package [pyrax]: pip install pyrax
- Python package [python-paramiko]: pip install python-paramiko
- ssh-keygen (provided with the openssh-client)
- python 2.7 or later
- If connecting to servers that already exist, you either need to know the password for the server and enter it when prompted or add your local user's ssh key [ $HOME/.ssh/id_rsa.pub ] to the remote server's [ /root/.ssh/authorized_keys ] file before running the program.

################################################################################
Configuration File Explained:
################################################################################

    {
    "username" : "USERNAME"
    , "apikey" : "API_KEY"
    , "servers" : {
        "web-servers-for-main-site" : {   <--- Any name to describe this group of servers
          "server_tag" : [ "web-prod" ]   <--- A list of servers or the prefix for the server names
          , "clone_slave_tag" : "web-prod-03"  <--- The exact server name which will be cloned as a "base" image
          , "load_balancer" : "lb-web-prod"  <--- Name of the load balancer that will be created and/or used
          , "min_servers" : 3   <--- The minimum number of servers to maintain
          , "min_loadavg" : 2   <--- If the loadavg drops below this level, start deleting "spare" servers
          , "max_loadavg" : 2.5   <--- If the loadavg goes above this level, start building servers from "base" image
          , "load_watch_time" : 10   <--- The time to wait to see if the load changes after min/max loadavg is hit
          , "interval" : 5   <--- How often to poll servers to get their load average
          , "build_server_increment" : 2   <--- How many "spare" servers to add when scaling up
          , "drop_server_decrement" : 1   <--- How many "spare" servers to delete when scaling down
        }
      }
    }

################################################################################
Other configuration file information:
################################################################################
      "server_tag" : [ "web-prod-01", "web-prod-02" ]  <--- Multiple items in the list specifies servers that exist. 
      Also, if you use a prefix that matches an existing server or have multiple items in the list, the "min_servers" 
      must be equal to the amount of servers that match. 
      If you already cloned other servers that have a similar naming convention then the "min_servers" value can be 
      greater than the servers listed. 

################################################################################


