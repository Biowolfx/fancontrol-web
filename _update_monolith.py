#!/usr/bin/env python3
"""Update monolith.py module sections from modular source files."""
import re

MONOLITH = '/home/impulse/fancontrol-web/monolith.py'

MODULE_MAP = {
    'core.state':       '/home/impulse/fancontrol-web/core/state.py',
    'core.config':      '/home/impulse/fancontrol-web/core/config.py',
    'core.hardware':    '/home/impulse/fancontrol-web/core/hardware.py',
    'core.dsm_fan':     '/home/impulse/fancontrol-web/core/dsm_fan.py',
    'core.control':     '/home/impulse/fancontrol-web/core/control.py',
    'server.node_registry': '/home/impulse/fancontrol-web/server/node_registry.py',
    'server.agent_handlers': '/home/impulse/fancontrol-web/server/agent_handlers.py',
    'server.routes':    '/home/impulse/fancontrol-web/server/routes.py',
    'agent.client':     '/home/impulse/fancontrol-web/agent/client.py',
    'app':              '/home/impulse/fancontrol-web/app.py',
}

STRIP_IMPORT_RE = re.compile(
    r'^(from\s+(core|server|agent|app)[.\w]*\s+import|import\s+(core|server|agent|app)[.\w]*)'
)


def strip_imports(content):
    """Remove inter-module imports, including multi-line ones."""
    lines = content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if STRIP_IMPORT_RE.match(stripped):
            # Check if this is a multi-line import (has unmatched parens)
            open_parens = stripped.count('(') - stripped.count(')')
            if open_parens > 0:
                # Skip until we close the parens
                i += 1
                while i < len(lines) and open_parens > 0:
                    open_parens += lines[i].count('(') - lines[i].count(')')
                    i += 1
                continue
            # Single-line import, just skip it
            i += 1
            continue
        result.append(line)
        i += 1
    return '\n'.join(result)


def read_module_source(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    content = strip_imports(content)
    lines = content.split('\n')
    # Skip shebang + docstring
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith('#!'):
            i += 1; continue
        if s.startswith('"""') or s.startswith("'''"):
            q = '"""' if s.startswith('"""') else "'''"
            if s.count(q) >= 2:
                i += 1; continue
            i += 1
            while i < len(lines) and q not in lines[i]:
                i += 1
            i += 1; continue
        if s == '':
            i += 1; continue
        break
    return '\n'.join(lines[i:])


def find_sections(lines):
    """Find all # MODULE: xxx section boundaries."""
    sections = []
    for i, line in enumerate(lines):
        m = re.match(r'^# MODULE:\s*(.+?)\s*$', line.strip())
        if m:
            marker = m.group(1)
            sep_start = i
            while sep_start > 0 and lines[sep_start - 1].strip().startswith('# =='):
                sep_start -= 1
            sep_end = i + 1
            while sep_end < len(lines) and lines[sep_end].strip().startswith('# =='):
                sep_end += 1
            sections.append((marker, sep_start, sep_end))
    
    result = []
    for idx, (marker, sep_start, sep_end) in enumerate(sections):
        if idx + 1 < len(sections):
            code_end = sections[idx + 1][1]
        else:
            code_end = len(lines)
            for j in range(sep_end, len(lines)):
                if 'EMBEDDED FRONTEND' in lines[j]:
                    code_end = j
                    break
        result.append((marker, sep_start, sep_end, code_end))
    return result


def main():
    with open(MONOLITH, 'r') as f:
        original = f.read()
    
    lines = original.split('\n')
    sections = find_sections(lines)
    
    print(f"Found {len(sections)} sections")
    
    new_lines = []
    prev_end = 0
    
    for marker, sep_start, sep_end, code_end in sections:
        new_lines.extend(lines[prev_end:sep_start])
        
        if marker in MODULE_MAP:
            new_code = read_module_source(MODULE_MAP[marker])
            new_lines.append('# ==============================================================================')
            new_lines.append(f'# MODULE: {marker}')
            new_lines.append('# ==============================================================================')
            new_lines.append('')
            new_lines.extend(new_code.rstrip('\n').split('\n'))
            new_lines.append('')
            print(f"  REPLACED {marker}")
        else:
            new_lines.extend(lines[sep_start:code_end])
            print(f"  KEPT {marker}")
        
        prev_end = code_end
    
    new_lines.extend(lines[prev_end:])
    
    # Insert core.update_helper before server.node_registry
    for i, line in enumerate(new_lines):
        if '# MODULE: server.node_registry' in line:
            sep_start = i
            while sep_start > 0 and new_lines[sep_start - 1].strip().startswith('# =='):
                sep_start -= 1
            uh_code = read_module_source('/home/impulse/fancontrol-web/core/update_helper.py')
            uh_lines = [
                '',
                '# ==============================================================================',
                '# MODULE: core.update_helper',
                '# ==============================================================================',
                '',
            ] + uh_code.rstrip('\n').split('\n') + ['']
            new_lines = new_lines[:sep_start] + uh_lines + new_lines[sep_start:]
            print("  INSERTED core.update_helper")
            break
    
    result = '\n'.join(new_lines)
    
    with open(MONOLITH, 'w') as f:
        f.write(result)
    
    print(f"\nDone! Original: {len(lines)} lines, New: {len(new_lines)} lines")


if __name__ == '__main__':
    main()
