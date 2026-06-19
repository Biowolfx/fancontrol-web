import os
import subprocess
import threading
import time
from flask import Flask, jsonify, request, render_template

app = Flask(__name__, template_folder='templates')

install_status = {
    'stage': 'idle',
    'message': '',
    'progress': 0,
    'complete': False,
    'error': None
}

def check_docker():
    try:
        subprocess.run(['docker', '--version'], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def run_install(config):
    global install_status
    try:
        install_status = {'stage': 'checking_docker', 'message': 'Checking Docker installation...', 'progress': 0, 'complete': False, 'error': None}
        
        if not check_docker():
            install_status = {'stage': 'error', 'message': 'Docker is not installed', 'progress': 0, 'complete': True, 'error': 'docker_not_found'}
            return
        
        install_status = {'stage': 'pulling_image', 'message': 'Pulling Docker image...', 'progress': 10, 'complete': False, 'error': None}
        
        image_pulled = False
        try:
            subprocess.run(['docker', 'pull', 'fancontrol-web'], check=True, capture_output=True, text=True)
            image_pulled = True
        except subprocess.CalledProcessError:
            pass
        
        if not image_pulled:
            install_status = {'stage': 'cloning_repo', 'message': 'Cloning repository...', 'progress': 20, 'complete': False, 'error': None}
            repo_dir = os.path.join(os.getcwd(), 'fancontrol-web')
            if not os.path.exists(repo_dir):
                subprocess.run(['git', 'clone', 'https://github.com/biowolfx/fancontrol-web.git'], check=True, cwd=os.getcwd())
        
        install_status = {'stage': 'generating_config', 'message': 'Generating docker-compose.yml...', 'progress': 50, 'complete': False, 'error': None}
        
        compose_content = generate_compose(config)
        compose_path = os.path.join(os.getcwd(), 'docker-compose.yml')
        with open(compose_path, 'w') as f:
            f.write(compose_content)
        
        install_status = {'stage': 'building', 'message': 'Building containers...', 'progress': 60, 'complete': False, 'error': None}
        
        if not image_pulled:
            subprocess.run(['docker', 'compose', 'build'], check=True, cwd=os.getcwd())
        
        install_status = {'stage': 'starting', 'message': 'Starting services...', 'progress': 80, 'complete': False, 'error': None}
        
        subprocess.run(['docker', 'compose', 'up', '-d'], check=True, cwd=os.getcwd())
        
        install_status = {'stage': 'complete', 'message': 'Installation complete!', 'progress': 100, 'complete': True, 'error': None}
        
    except Exception as e:
        install_status = {'stage': 'error', 'message': str(e), 'progress': install_status['progress'], 'complete': True, 'error': str(e)}

def generate_compose(config):
    mode = config.get('mode', 'server')
    lang = config.get('lang', 'en')
    port = config.get('port', 5059)
    data_dir = config.get('data_dir', './data')
    
    compose = {
        'version': '3.8',
        'services': {
            'fancontrol': {
                'image': 'fancontrol-web' if mode == 'server' else 'fancontrol-web:agent',
                'container_name': f'fancontrol-{mode}',
                'restart': 'unless-stopped',
                'ports': [f'{port}:5059'] if mode == 'server' else [],
                'volumes': [f'{data_dir}:/app/data'],
                'environment': [
                    f'FC_LANG={lang}',
                    f'FC_MODE={mode}'
                ]
            }
        }
    }
    
    if mode == 'agent':
        compose['services']['fancontrol']['environment'].extend([
            f'FC_SERVER_URL={config.get("server_url", "")}',
            f'FC_API_TOKEN={config.get("api_token", "")}',
            f'FC_NODE_NAME={config.get("node_name", "")}'
        ])
    
    yaml_content = "version: '3.8'\n\nservices:\n  fancontrol:\n"
    if mode == 'server':
        yaml_content += f"    image: fancontrol-web\n"
        yaml_content += f"    container_name: fancontrol-{mode}\n"
        yaml_content += f"    restart: unless-stopped\n"
        yaml_content += f"    ports:\n      - \"{port}:5059\"\n"
    else:
        yaml_content += f"    image: fancontrol-web:agent\n"
        yaml_content += f"    container_name: fancontrol-{mode}\n"
        yaml_content += f"    restart: unless-stopped\n"
    
    yaml_content += f"    volumes:\n      - \"{data_dir}:/app/data\"\n"
    yaml_content += f"    environment:\n"
    yaml_content += f"      - FC_LANG={lang}\n"
    yaml_content += f"      - FC_MODE={mode}\n"
    
    if mode == 'agent':
        yaml_content += f"      - FC_SERVER_URL={config.get('server_url', '')}\n"
        yaml_content += f"      - FC_API_TOKEN={config.get('api_token', '')}\n"
        yaml_content += f"      - FC_NODE_NAME={config.get('node_name', '')}\n"
    
    return yaml_content

@app.route('/')
def index():
    return render_template('setup.html')

@app.route('/api/status')
def status():
    return jsonify(install_status)

@app.route('/api/install', methods=['POST'])
def install():
    global install_status
    if install_status['stage'] != 'idle' and not install_status['complete']:
        return jsonify({'error': 'Installation already in progress'}), 400
    
    config = request.json
    thread = threading.Thread(target=run_install, args=(config,))
    thread.start()
    return jsonify({'status': 'started'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5060, debug=True)