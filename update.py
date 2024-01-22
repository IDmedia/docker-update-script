import os
import re
import sys
import json
import argparse
import subprocess
from collections import Counter


def get_image_ids(compose_file):
    # Get the image IDs from the specified docker-compose file
    output = subprocess.check_output(['docker-compose', '-f', compose_file, 'images', '-q']).decode().strip()
    return output.splitlines()


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
        # Handle the case where the docker command fails
        return None


def build_in_docker_compose(docker_compose_file_path):
    # Check if 'build' is present and not commented out in a Docker Compose file
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
                    print(f'Docker login failed with exit code {e.returncode}')
                    print('An error occurred during the login process. Please check your credentials.')
                    sys.exit(1)

    return docker_registries


def main(args):
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
        docker_file = os.path.join(docker_dir, container, 'Dockerfile')

        # Process each valid docker-compose file
        if not os.path.isfile(compose_file):
            continue

        # Get current image id
        current_image_ids = get_image_ids(compose_file)

        # Build or pull the latest image
        if os.path.isfile(docker_file) and build_in_docker_compose(compose_file):
            subprocess.check_call(['docker-compose', '-f', compose_file, 'build', '--no-cache'])
        else:
            subprocess.check_call(['docker-compose', '-f', compose_file, 'pull'])

        # Check the new tag the container has
        new_image_ids = get_image_ids(compose_file)
        new_image_ids = [image_id for image_id in new_image_ids if get_docker_tag(image_id) is not None]

        # Check if the image IDs have changed or force re-creation
        if (Counter(current_image_ids) != Counter(new_image_ids)) or args.force:
            containers_restart.append(compose_file)

    # Restart selected containers
    for compose_file in containers_restart:

        # Remove containers
        subprocess.check_call(['docker-compose', '-f', compose_file, 'down', '-t', str(args.timeout)])

        # Start containers
        subprocess.check_call(['docker-compose', '-f', compose_file, 'up', '-d'])

    # Docker logout from authenticated registries
    for registry in docker_registries:
        subprocess.run(f'docker logout {registry}', shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Docker prune unused resources
    subprocess.check_call(['docker', 'image', 'prune', '-f'])
    subprocess.check_call(['docker', 'volume', 'prune', '-f'])
    subprocess.check_call(['docker', 'builder', 'prune', '-f'])
    subprocess.check_call(['docker', 'network', 'prune', '-f'])


if __name__ == '__main__':
    # Command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--containers', type=str, help='Specify a list of containers to update (example: "couchpotato, medusa")', required=False)
    parser.add_argument('-e', '--exclude', type=str, help='Specify a list of containers to exclude from the update (example: "sonarr, radarr")', required=False)
    parser.add_argument('-f', '--force', default=False, action='store_true', help='Force re-creating container(s)')
    parser.add_argument('-t', '--timeout', type=int, default=60, help='Specify the timeout for stopping containers (default: 60)')
    args = parser.parse_args()

    # Execute the main function with the parsed arguments
    main(args)
