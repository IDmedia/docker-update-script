import os
import re
import sys
import json
import logging
import argparse
import subprocess
from collections import Counter


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


def get_image_ids(compose_file):
    # Get the image IDs from the specified docker compose files (base + override if present)
    output = subprocess.check_output(
        ['docker', 'compose', *compose_file_flags(compose_file), 'images', '-q']
    ).decode().strip()
    return output.splitlines()


def get_docker_container_state(container_id):
    try:
        # Run the docker inspect command and capture the output
        output = subprocess.check_output(['docker', 'inspect', container_id], universal_newlines=True)
        # Parse the JSON output
        container_info = json.loads(output)
        # Extract the state from the output
        if container_info:
            # The state is available under the 'State' key
            state = container_info[0].get('State', {})
            if state:
                return state
        # No container found with the given ID or no state available
        return None
    except subprocess.CalledProcessError:
        logger.error(f"Failed to retrieve container state from container id '{container_id}'")
        return None


def get_docker_container_state_from_compose(yaml_path):
    try:
        # Run the docker compose ps -q command to get container IDs (base + override if present)
        output = subprocess.check_output(
            ['docker', 'compose', *compose_file_flags(yaml_path), 'ps', '-q'],
            universal_newlines=True
        )
        # Split the output into lines and remove empty lines
        container_ids = [line.strip() for line in output.splitlines() if line.strip()]
        container_states = []
        # Loop through container IDs and get their states
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
        # Run the docker inspect command and capture the output
        output = subprocess.check_output(['docker', 'inspect', image_sha], universal_newlines=True)
        # Parse the JSON output
        image_info = json.loads(output)
        # Extract the tag from the output
        if image_info:
            # The tags are available under the 'RepoTags' key
            tags = image_info[0].get('RepoTags', [])
            if tags:
                return tags[0].split(':')[1]
        # No image found with the given SHA or no tags available
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
        # Simple check: presence of 'build:' in the rendered config
        return re.search(r'^\s*build:', cfg, re.MULTILINE) is not None
    except subprocess.CalledProcessError:
        # Fallback: check only the base file content
        with open(docker_compose_file_path, 'r') as file:
            content = file.read()
        build_pattern = re.compile(r'^\s*build:', re.MULTILINE)
        comment_pattern = re.compile(r'^\s*#.*build:', re.MULTILINE)
        return len(re.findall(build_pattern, content)) > len(re.findall(comment_pattern, content))


def authenticate_docker_registries():
    # Authenticate to Docker registries using credentials from a JSON file
    docker_registries = []
    docker_login_json_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), '.docker-update')
    if os.path.exists(docker_login_json_file):
        with open(docker_login_json_file, 'r') as file:
            data = json.load(file)
            for entry in data:
                registry, credentials = entry.popitem()
                username, password = credentials['username'], credentials['password']
                try:
                    # Docker login command
                    subprocess.run(f'echo "{password}" | docker login --username "{username}" --password-stdin "{registry}"',
                                   shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    docker_registries.append(registry)
                except subprocess.CalledProcessError as e:
                    logger.error(f'Docker login failed with exit code {e.returncode}')
                    logger.error('An error occurred during the login process. Please check your credentials.')
                    sys.exit(1)
    return docker_registries


def restart_container(compose_file, timeout):
    # Remove containers
    subprocess.check_call([
        'docker', 'compose', *compose_file_flags(compose_file),
        'down', '--remove-orphans', '-t', str(timeout)
    ])

    # Start containers
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
    # Ensure `docker compose` is available
    check_docker_compose_version()

    # Get the absolute path of the script directory
    docker_dir = os.path.dirname(os.path.realpath(__file__))
    containers = args.containers

    # Get the list of containers to update
    if not containers:
        containers = [d for d in sorted(os.listdir(docker_dir)) if os.path.isdir(d) and ('@' or '.') not in d]
    else:
        containers = [name.strip() for name in containers.split(',')]

    # Exclude containers
    if args.exclude:
        containers_exclude = [name.strip() for name in args.exclude.split(',')]
        containers = [container for container in containers if container not in containers_exclude]

    # Authenticate to Docker registries
    docker_registries = authenticate_docker_registries()

    # Containers to restart
    containers_restart = []
    for container in containers:
        compose_file = os.path.join(docker_dir, container, 'docker-compose.yaml')
        # Process each valid docker-compose file
        if not os.path.isfile(compose_file):
            logger.warning(f"Skipping container '{container}' because docker-compose.yaml is missing")
            continue
        # Get current image id
        logger.info(f"Updating '{container}'")
        current_image_ids = get_image_ids(compose_file)
        # Build or pull the latest image
        if build_in_docker_compose(compose_file):
            logger.info(f"Initiating build of '{container}' specified by 'build' in compose files")
            subprocess.check_call(['docker', 'compose', *compose_file_flags(compose_file), 'build', '--no-cache'])
        else:
            logger.info(f"Pulling new image for '{container}'")
            subprocess.check_call(['docker', 'compose', *compose_file_flags(compose_file), 'pull'])
        # Check the new tag the container has
        new_image_ids = get_image_ids(compose_file)
        new_image_ids = [image_id for image_id in new_image_ids if get_docker_tag(image_id) is not None]
        # Check if the image IDs have changed or force re-creation
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
    for registry in docker_registries:
        logger.info(f"Logging out from Docker registry: {registry}")
        subprocess.run(f'docker logout {registry}', shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Docker prune unused resources
    if not args.immediate:
        prune_resources()


if __name__ == '__main__':
    # Define color codes
    COLORS = {
        'INFO': '\033[0m',     # White
        'WARNING': '\033[93m',  # Yellow
        'ERROR': '\033[91m',    # Red
        'CRITICAL': '\033[91m'  # Red
    }
    RESET_COLOR = '\033[0m'  # Reset color to default

    # Custom formatter
    class ColoredFormatter(logging.Formatter):
        def format(self, record):
            levelname = record.levelname
            msg = super().format(record)
            color = COLORS.get(levelname, '')
            return f"{color}{msg}{RESET_COLOR}"

    # Remove basicConfig() and directly configure the root logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    # Add colored formatter to the default stream handler
    handler = logging.StreamHandler()
    formatter = ColoredFormatter('%(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--containers', type=str, help='Specify a list of containers to update (example: "couchpotato, medusa")', required=False)
    parser.add_argument('-e', '--exclude', type=str, help='Specify a list of containers to exclude from the update (example: "sonarr, radarr")', required=False)
    parser.add_argument('-f', '--force', default=False, action='store_true', help='Force re-creating container(s)')
    parser.add_argument('-i', '--immediate', action='store_true', help='Update, restart and prune immediately per container to save space')
    parser.add_argument('-t', '--timeout', type=int, default=60, help='Specify the timeout for stopping containers (default: 60)')
    args = parser.parse_args()

    # Execute the main function with the parsed arguments
    main(args)
