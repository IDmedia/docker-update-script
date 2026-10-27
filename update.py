import os
import re
import sys
import json
import logging
import argparse
import subprocess
from collections import Counter

# Registry credentials, first existing file wins. '.docker-update' is the legacy name and may be YAML or JSON.
CONFIG_FILES = ('.docker-update.yaml', '.docker-update.yml', '.docker-update')


def check_docker_compose_version():
    """Ensure that `docker compose` (v2) is available."""
    try:
        subprocess.check_output(['docker', 'compose', 'version'], universal_newlines=True)
        return True
    except subprocess.CalledProcessError:
        logger.error("Docker Compose v2 (`docker compose`) is not installed or not available.")
        sys.exit(1)


def compose_file_flags(compose_file):
    """Return ['-f', base, '-f', override] if override exists, otherwise ['-f', base]."""
    flags = ['-f', compose_file]
    override = os.path.join(os.path.dirname(compose_file), 'docker-compose.override.yaml')
    if os.path.isfile(override):
        flags += ['-f', override]
    return flags


def get_image_names(compose_file):
    """Return the image names declared in the compose file(s) via `docker compose config --images`."""
    images_output = subprocess.check_output(
        ['docker', 'compose', *compose_file_flags(compose_file), 'config', '--images'],
        universal_newlines=True
    ).strip()
    return [name for name in set(images_output.splitlines()) if name]


def get_image_registry(image_name):
    """Return the registry host of an image name, e.g. 'registry.domain.ltd/group/app:latest' -> 'registry.domain.ltd'."""
    first, separator, _ = image_name.partition('/')
    if separator and ('.' in first or ':' in first or first == 'localhost'):
        return first
    return 'docker.io'


def get_image_ids(compose_file):
    """
    Resolve the image IDs that the compose file's image tags currently point to
    in the local Docker store. Uses `docker compose config --images` to get the
    declared image names, then `docker inspect` to resolve each to its current
    local SHA.
    """
    ids = []
    for name in get_image_names(compose_file):
        try:
            sha = subprocess.check_output(
                ['docker', 'inspect', '--format', '{{.Id}}', name],
                universal_newlines=True
            ).strip()
            if sha:
                ids.append(sha)
        except subprocess.CalledProcessError:
            ids.append(f'missing:{name}')
    return ids


def get_docker_container_state(container_id):
    try:
        output = subprocess.check_output(['docker', 'inspect', container_id], universal_newlines=True)
        container_info = json.loads(output)
        if container_info:
            state = container_info[0].get('State', {})
            if state:
                return state
        return None
    except subprocess.CalledProcessError:
        logger.error(f"Failed to retrieve container state from container id '{container_id}'")
        return None


def get_docker_container_state_from_compose(yaml_path):
    try:
        output = subprocess.check_output(
            ['docker', 'compose', *compose_file_flags(yaml_path), 'ps', '-q'],
            universal_newlines=True
        )
        container_ids = [line.strip() for line in output.splitlines() if line.strip()]
        container_states = []
        for container_id in container_ids:
            container_state = get_docker_container_state(container_id)
            if container_state:
                container_states.append(container_state)
        return container_states
    except subprocess.CalledProcessError:
        logger.error(f"Failed to retrieve container state from docker-compose file '{yaml_path}'")
        return None


def get_docker_tag(image_sha):
    try:
        output = subprocess.check_output(['docker', 'inspect', image_sha], universal_newlines=True)
        image_info = json.loads(output)
        if image_info:
            tags = image_info[0].get('RepoTags', [])
            if tags:
                return tags[0].split(':')[1]
        return None
    except subprocess.CalledProcessError:
        logger.error(f"Failed to retrieve container tag from image SHA '{image_sha}'")
        return None


def build_in_docker_compose(docker_compose_file_path):
    """
    Determine if any service is defined with 'build' in the merged config (base + override if present).
    """
    try:
        cfg = subprocess.check_output(
            ['docker', 'compose', *compose_file_flags(docker_compose_file_path), 'config'],
            universal_newlines=True
        )
        return re.search(r'^\s*build:', cfg, re.MULTILINE) is not None
    except subprocess.CalledProcessError:
        with open(docker_compose_file_path, 'r') as file:
            content = file.read()
        build_pattern = re.compile(r'^\s*build:', re.MULTILINE)
        comment_pattern = re.compile(r'^\s*#.*build:', re.MULTILINE)
        return len(re.findall(build_pattern, content)) > len(re.findall(comment_pattern, content))


def load_registry_credentials():
    """
    Load registry credentials from '.docker-update.yaml', '.docker-update.yml' or '.docker-update'
    (YAML, or the legacy JSON list format). Returns {registry: [{'username': ..., 'password': ...}, ...]}.
    """
    script_dir = os.path.dirname(os.path.realpath(__file__))
    config_file = next(
        (path for path in (os.path.join(script_dir, name) for name in CONFIG_FILES) if os.path.isfile(path)),
        None
    )
    if not config_file:
        return {}

    with open(config_file, 'r') as file:
        content = file.read()
    try:
        data = json.loads(content)
    except ValueError:
        try:
            import yaml
        except ImportError:
            logger.error(f"PyYAML is required to read '{config_file}'. Install it with 'pip3 install pyyaml'.")
            sys.exit(1)
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse '{config_file}': {e}")
            sys.exit(1)

    # Legacy JSON format: [{"registry": {"username": ..., "password": ...}}, ...]
    if isinstance(data, list):
        legacy = {}
        for entry in data:
            for registry, login in entry.items():
                legacy.setdefault(registry, []).append(login)
        data = legacy

    credentials = {}
    for registry, logins in (data or {}).items():
        if isinstance(logins, dict):
            logins = [logins]
        try:
            credentials[str(registry)] = [
                {'username': str(login['username']), 'password': str(login['password'])} for login in logins or []
            ]
        except (KeyError, TypeError):
            logger.error(f"Invalid login for registry '{registry}' in '{config_file}': 'username' and 'password' are required")
            sys.exit(1)
    return credentials


class RegistryAuth:
    """
    Keeps track of Docker registry logins. Docker stores only one credential per registry host,
    so when a registry has multiple logins only one of them can be active at a time.
    """

    def __init__(self, credentials):
        self.credentials = credentials
        self.active = {}  # registry -> index of the currently active login
        self.logged_in = set()  # registries to log out from when done

    def login(self, registry, index):
        logins = self.credentials[registry]
        username, password = logins[index]['username'], logins[index]['password']
        try:
            subprocess.run(
                ['docker', 'login', '--username', username, '--password-stdin', registry],
                input=password, universal_newlines=True, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError as e:
            logger.warning(
                f"Docker login {index + 1}/{len(logins)} ('{username}') failed for registry '{registry}' "
                f"with exit code {e.returncode}"
            )
            return False
        self.active[registry] = index
        self.logged_in.add(registry)
        logger.info(f"Successfully authenticated to registry: {registry} (login {index + 1}/{len(logins)}, '{username}')")
        return True

    def login_all(self):
        """Log in to every registry with its first working login. Returns the registries where all logins failed."""
        failed_registries = []
        for registry, logins in self.credentials.items():
            if not any(self.login(registry, index) for index in range(len(logins))):
                failed_registries.append(registry)
        return failed_registries

    def switch_logins(self, registries):
        """Activate each alternative login for the given registries in turn, yielding (registry, index) after each switch."""
        for registry in registries:
            if registry not in self.active:
                continue
            original = self.active[registry]
            for index in range(len(self.credentials[registry])):
                if index != original and self.login(registry, index):
                    yield registry, index

    def logout_all(self):
        for registry in sorted(self.logged_in):
            logger.info(f"Logging out from Docker registry: {registry}")
            subprocess.run(['docker', 'logout', registry], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_with_login_fallback(command, registries, auth):
    """
    Run a pull/build command. If it fails, retry it after switching to each alternative login of the
    given registries. Raises the last CalledProcessError if every attempt fails.
    """
    try:
        subprocess.check_call(command)
        return
    except subprocess.CalledProcessError as e:
        last_error = e
    for registry, index in auth.switch_logins(registries):
        logger.info(f"Retrying with login {index + 1}/{len(auth.credentials[registry])} for registry '{registry}'")
        try:
            subprocess.check_call(command)
            return
        except subprocess.CalledProcessError as e:
            last_error = e
    raise last_error


def restart_container(compose_file, timeout):
    subprocess.check_call([
        'docker', 'compose', *compose_file_flags(compose_file),
        'down', '--remove-orphans', '-t', str(timeout)
    ])
    subprocess.check_call(['docker', 'compose', *compose_file_flags(compose_file), 'up', '-d'])


def prune_resources():
    logger.info('Pruning unused Docker resources')
    try:
        subprocess.check_call(['docker', 'image', 'prune', '-f'])
        subprocess.check_call(['docker', 'volume', 'prune', '-f'])
        subprocess.check_call(['docker', 'builder', 'prune', '-f'])
        subprocess.check_call(['docker', 'network', 'prune', '-f'])
    except subprocess.CalledProcessError:
        logger.warning("Failed to prune some resources.")


def main(args):
    check_docker_compose_version()

    docker_dir = os.path.dirname(os.path.realpath(__file__))
    containers = args.containers

    if not containers:
        containers = [d for d in sorted(os.listdir(docker_dir)) if os.path.isdir(d) and ('@' or '.') not in d]
    else:
        containers = [name.strip() for name in containers.split(',')]

    if args.exclude:
        containers_exclude = [name.strip() for name in args.exclude.split(',')]
        containers = [container for container in containers if container not in containers_exclude]

    # Authenticate to Docker registries
    auth = RegistryAuth(load_registry_credentials())
    failed_registries = auth.login_all()
    if failed_registries:
        logger.warning(
            f"The following registries failed authentication and are considered unavailable: "
            f"{', '.join(failed_registries)}"
        )

    skipped_containers = []  # Track containers skipped due to errors
    containers_restart = []

    for container in containers:
        compose_file = os.path.join(docker_dir, container, 'docker-compose.yaml')

        if not os.path.isfile(compose_file):
            logger.warning(f"Skipping container '{container}' because docker-compose.yaml is missing")
            continue

        logger.info(f"Updating '{container}'")
        current_image_ids = get_image_ids(compose_file)

        # Build or pull the latest image — skip container gracefully on failure
        try:
            if build_in_docker_compose(compose_file):
                logger.info(f"Initiating build of '{container}' specified by 'build' in compose files")
                # Base images live in the Dockerfile, so any registry may be involved
                run_with_login_fallback(
                    ['docker', 'compose', *compose_file_flags(compose_file), 'build', '--no-cache'],
                    auth.credentials, auth
                )
            else:
                logger.info(f"Pulling new image for '{container}'")
                run_with_login_fallback(
                    ['docker', 'compose', *compose_file_flags(compose_file), 'pull'],
                    {get_image_registry(name) for name in get_image_names(compose_file)}, auth
                )
        except subprocess.CalledProcessError:
            logger.warning(
                f"Skipping '{container}': failed to build/pull image. "
                f"The registry may be unavailable or a network error occurred."
            )
            skipped_containers.append(container)
            continue  # Move on to the next container

        new_image_ids = get_image_ids(compose_file)

        if (Counter(current_image_ids) != Counter(new_image_ids)) or args.force:
            if args.force:
                logger.warning(f"Adding '{container}' to restart list (forced)")
            else:
                logger.warning(f"Adding '{container}' to restart list as a newer version is available")

            if args.immediate:
                restart_container(compose_file, args.timeout)
                prune_resources()
            else:
                containers_restart.append(compose_file)
        else:
            logger.info(f"Image for '{container}' remains unchanged, no container restart required")

    # Restart selected containers
    for compose_file in containers_restart:
        restart_container(compose_file, args.timeout)

    # Docker logout from authenticated registries
    auth.logout_all()

    # Prune unused resources
    if not args.immediate:
        prune_resources()

    # Final summary of skipped containers
    if skipped_containers:
        logger.warning(f"The following containers were skipped due to errors: {', '.join(skipped_containers)}")


if __name__ == '__main__':
    COLORS = {
        'INFO': '\033[0m',
        'WARNING': '\033[93m',
        'ERROR': '\033[91m',
        'CRITICAL': '\033[91m'
    }
    RESET_COLOR = '\033[0m'

    class ColoredFormatter(logging.Formatter):
        def format(self, record):
            levelname = record.levelname
            msg = super().format(record)
            color = COLORS.get(levelname, '')
            return f"{color}{msg}{RESET_COLOR}"

    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler()
    formatter = ColoredFormatter('%(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--containers', type=str, help='Specify a list of containers to update (example: "couchpotato, medusa")', required=False)
    parser.add_argument('-e', '--exclude', type=str, help='Specify a list of containers to exclude from the update (example: "sonarr, radarr")', required=False)
    parser.add_argument('-f', '--force', default=False, action='store_true', help='Force re-creating container(s)')
    parser.add_argument('-i', '--immediate', action='store_true', help='Update, restart and prune immediately per container to save space')
    parser.add_argument('-t', '--timeout', type=int, default=60, help='Specify the timeout for stopping containers (default: 60)')
    args = parser.parse_args()

    main(args)
