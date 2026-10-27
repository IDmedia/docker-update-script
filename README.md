<div align="center">
  <img src="extras/logo.png" width="250" alt="logo">
</div>


# Docker Update Script
This Python script streamlines the update of Docker containers specified in Docker Compose files. With a single command, it efficiently handles the task of building or pulling the latest Docker images and restarts containers only when required.


## Prerequisites

The script anticipates a directory arrangement where each service resides within its individual sub-directory. The script itself is positioned at the root level of the directory structure.
```
.
└── Docker/
    ├── CouchPotato/
    │   └── docker-compose.yaml
    ├── Medusa/
    │   └── docker-compose.yaml
    ├── CustomApp/
    │   ├── docker-compose.yaml
    │   └── Dockerfile
    ├── docker-update.py
    └── .docker-update (optional)
```


## Usage
```
python3 docker_update_script.py [-h] [-c CONTAINERS] [-e EXCLUDE] [-f] [-t TIMEOUT]
```

* -c, --containers: Specify a list of containers to update (e.g., "couchpotato, medusa").
* -e, --exclude: Specify a list of containers to exclude from the update (e.g., "sonarr, radarr").
* -f, --force: Force re-creating container(s).
* -t, --timeout: Specify the timeout for stopping containers (default: 60).


## Examples
Update specific containers, restarting only if necessary:
```
python3 docker_update_script.py -c "sonarr, radarr"
```
Exclude containers from the update:
```
python3 docker_update_script.py -e "traefik, ddclient"
```
Force re-creation of containers:
```
python3 docker_update_script.py -f
```
Set a custom timeout for stopping containers (e.g., 30 seconds):
```
python3 docker_update_script.py -t 30
```


## Private registries authentication
For users employing one or more private registries, support for authentication can be added by creating an INI file named ".docker-update.ini" next to the script. Each section is named after the registry; to add more logins for the same registry, append a label after the registry name. Lines starting with `#` or `;` are comments.
```ini
# Single login for a registry
[first_registry.domain.ltd]
username = USERNAME
password = PASSWORD

# Multiple logins for the same registry, e.g. deploy tokens for different groups/projects
# Deploy token for the "apps" group
[second_registry.domain.ltd apps]
username = USERNAME
password = PASSWORD

# Deploy token for the "tools" group
[second_registry.domain.ltd tools]
username = USERNAME
password = PASSWORD
```

Docker only keeps one credential per registry, so when a registry has multiple logins the script logs in with the first one that works. If pulling (or building) a container fails, it switches to each of the other logins for the registries that container uses and tries again. A single compose file that needs images requiring two different logins from the same registry at the same time is not supported.

The legacy JSON format in a file named ".docker-update" is still supported.


## Alias Configuration for unRAID
I utilize this script on my unRAID machine, opting for Docker Compose over container templates. If you find yourself in a similar situation, ensure the installation of the following plugins:
* Python 3
* User Scripts
* Compose.Manager

To set up an alias, follow these steps using the "User Scripts" plugin:

Create a New Script:
* Name: Alias docker-update
* Description: Update all Docker containers using docker-update
* Schedule: At First Array Start Only
* Script (remember to change the "docker"-path if necessary):
```
#!/bin/bash

if ! grep -q "alias docker-update" "/root/.bash_profile"; then
    echo 'alias docker-update="cd /mnt/user/docker/ && python3 update.py"' >> /root/.bash_profile
fi

source /root/.bash_profile
```
Make sure to modify the path to the top-level Docker container (/mnt/user/docker/) according to your setup. This alias simplifies the update process for all Docker containers on your unRAID machine. Now, you can utilize `docker-update` in place of `python3 docker_update_script.py`.


## Requirements
* Python 3.x
* Docker
* Docker Compose


### Feel free to contribute, report issues, or suggest improvements! If you find this repository useful, don't forget to star it :)

<a href="https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=JPGHGTWP33A5L">
  <img src="https://raw.githubusercontent.com/stefan-niedermann/paypal-donate-button/master/paypal-donate-button.png" alt="Donate with PayPal" />
</a>
