# mcp_server.py
import asyncio
import json
import time
import logging
import sys
from pathlib import Path
import json
from datetime import datetime
from typing import Dict, List, Optional, Set
from aiohttp import web
from windowManager.window_manager import WindowManager
from windowManager.window_functions import WindowController
from windowManager.window_screengrab import take_screenshot

def setup_logging(module_name: str):
    """
    Simple logging setup with both file and console output
    Args:
        module_name: Name of the module for log messages
    """
    # Create logs directory if it doesn't exist
    log_dir = Path(__file__).parent.parent / 'logs'
    log_dir.mkdir(exist_ok=True)
    
    # Common log file path
    log_file = log_dir / 'mcp_server_mcp_control.log'

    # Format to include timestamp, level, module name, function name, line number
    log_format = '%(asctime)s - %(levelname)s - %(module)s:%(funcName)s:%(lineno)d - %(message)s'
    
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.FileHandler(log_file, mode='w', encoding='utf-8'),
            #logging.StreamHandler(sys.stdout)
        ]
    )

    return logging.getLogger(module_name)

logger = setup_logging("MCPServer")

class MCPServer:
    def __init__(self):
        logger.info("Initializing MCPServer")
        self.wm = WindowManager()
        self.wc = WindowController()
        self.clients: Set[web.StreamResponse] = set()
        self.command_history = []
        self.max_history = 100
        self._running = True
        self.window_short_id_lookup = {}  # NEW: short_id -> full_id

    def refresh_window_short_id_lookup(self):
        """Refresh the short ID lookup table from current windows."""
        data = self.wm.get_structured_windows()
        lookup = {}
        for monitor_data in data["monitors"].values():
            for app_data in monitor_data["applications"].values():
                for window_id, window_data in app_data["windows"].items():
                    last_8 = window_id[-8:]
                    lookup[last_8] = window_id
        self.window_short_id_lookup = lookup

    async def handle_sse(self, request):
        logger.info("SSE client connected")
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'Access-Control-Allow-Origin': '*',  # For development
            }
        )
        
        try:
            await response.prepare(request)
            self.clients.add(response)
            
            # Send initial state
            try:
                await self._send_event(response, 'init', {
                    'status': 'connected',
                    'tools': self._get_available_tools()
                })
            except Exception as e:
                print(f"Error sending initial state: {e}")
                self.clients.discard(response)
                return response

            # Keep connection alive
            while self._running:
                try:
                    await asyncio.sleep(1)
                except Exception as e:
                    print(f"SSE connection error: {e}")
                    break

        except Exception as e:
            print(f"SSE setup error: {e}")
        finally:
            # Remove client from set
            self.clients.discard(response)
            try:
                await response.write_eof()
            except Exception as e:
                print(f"Error closing SSE connection: {e}")
            return response

    async def handle_command(self, request):
        logger.debug("Received command request")
        try:
            data = await request.json()
            command = data.get('command')
            params = data.get('params', {})
            logger.info(f"Executing command: {command} with params: {params}")

            if not command:
                logger.warning("No command provided in request")
                return web.json_response({'error': 'No command provided'}, status=400)
            
            # Execute command
            result = await self._execute_command(command, params)
            logger.info(f"Command result: {result}")
            
            # Add to history
            self.command_history.append({
                'command': command,
                'params': params,
                'result': result
            })
            if len(self.command_history) > self.max_history:
                self.command_history.pop(0)
            
            # Broadcast result to all clients
            await self._broadcast_event('command_result', {
                'command': command,
                'params': params,
                'result': result
            })
            
            return web.json_response(result)
        except Exception as e:
            logger.exception("Exception in handle_command")
            return web.json_response({'error': str(e)}, status=500)

    async def handle_tools(self, request):
        """Return available tools"""
        return web.json_response(self._get_available_tools())

    async def handle_history(self, request):
        """Return command history"""
        return web.json_response(self.command_history)

    def _get_available_tools(self) -> Dict:
        """Get list of available tools and their parameters"""
        return {
            'window_commands': {
                'maximize': {'description': 'Maximize window', 'params': {'window_id': 'string'}},
                'minimize': {'description': 'Minimize window', 'params': {'window_id': 'string'}},
                'close': {'description': 'Close window', 'params': {'window_id': 'string'}},
                'resize': {'description': 'Resize window', 'params': {'window_id': 'string', 'width': 'number', 'height': 'number'}},
                'move': {'description': 'Move window', 'params': {'window_id': 'string', 'x': 'number', 'y': 'number'}},
                'screen': {'description': 'Move to screen position', 'params': {'window_id': 'string', 'screen': 'number', 'x': 'number', 'y': 'number'}},
                'monitor': {'description': 'Move to monitor', 'params': {'window_id': 'string', 'monitor': 'number'}},
                'introspect': {'description': 'Deep window introspection', 'params': {'window_id': 'string'}},
                'tree': {'description': 'Show UI hierarchy tree', 'params': {'window_id': 'string'}},
                'get_windows': {'description': 'Get all windows', 'params': {'show_minimized': 'boolean'}},
                'print_windows_summary': {'description': 'Print summary of all windows', 'params': {}},
                'refresh_windows': {'description': 'Refresh window list', 'params': {}}
            },
            'mouse_commands': {
                'click': {'description': 'Mouse click', 'params': {'button': 'string', 'x': 'number', 'y': 'number'}},
                'doubleclick': {'description': 'Double click', 'params': {'button': 'string', 'x': 'number', 'y': 'number'}},
                'longclick': {'description': 'Long click', 'params': {'button': 'string', 'duration': 'number', 'x': 'number', 'y': 'number'}},
                'scroll': {'description': 'Scroll', 'params': {'direction': 'string', 'amount': 'number', 'x': 'number', 'y': 'number'}},
                'drag': {'description': 'Drag', 'params': {'start_x': 'number', 'start_y': 'number', 'end_x': 'number', 'end_y': 'number', 'button': 'string', 'duration': 'number'}}
            },
            'keyboard_commands': {
                'send': {'description': 'Send key combination', 'params': {'keys': 'string'}},
                'type': {'description': 'Type text', 'params': {'text': 'string'}}
            },
            'system_commands': {
                'launch': {'description': 'Launch application', 'params': {'app_name': 'string', 'screen_id': 'number', 'fullscreen': 'boolean'}},
                'msgbox': {'description': 'Show message box', 'params': {'title': 'string', 'message': 'string', 'x': 'number', 'y': 'number'}},
                'computer': {'description': 'Get computer name', 'params': {}},
                'user': {'description': 'Get user name', 'params': {}},
                'keys': {'description': 'Show virtual key codes', 'params': {}},
                'hover': {'description': 'Analyze element under mouse cursor', 'params': {}},
                'inspect': {'description': 'Full window analysis (same as i)', 'params': {}},
                'detect': {'description': 'Real-time cursor element detection', 'params': {}},
                'cursor': {'description': 'Get or set cursor position', 'params': {'x': 'number (optional)', 'y': 'number (optional)'}},
            }
        }

    async def _execute_command(self, command: str, params: Dict) -> Dict:
        logger.info(f"Executing _execute_command: {command} {params}")
        try:
            # Parse command and parameters
            if command in self._get_available_tools()['window_commands']:
                return await self._execute_window_command(command, params)
            elif command in self._get_available_tools()['mouse_commands']:
                return await self._execute_mouse_command(command, params)
            elif command in self._get_available_tools()['keyboard_commands']:
                return await self._execute_keyboard_command(command, params)
            elif command in self._get_available_tools()['system_commands']:
                return await self._execute_system_command(command, params)
            else:
                return {'error': f'Unknown command: {command}'}
        except Exception as e:
            return {'error': str(e)}

    async def _execute_window_command(self, command: str, params: Dict) -> Dict:
        """Execute window-related command, supporting short window IDs."""
        logger.info(f"Executing _execute_window_command: {command} {params}")
        try:
            self.refresh_window_short_id_lookup()  # Always refresh before command

            if command == 'get_windows':
                # Use get_all_windows instead of get_windows
                windows = self.wm.get_all_windows()
                return {
                    'success': True,
                    'result': {  # Changed to match expected format
                        'windows': windows
                    },
                    'message': f'Found {len(windows)} windows'
                }
            elif command == 'print_windows_summary':
                data = self.wm.get_structured_windows()
                summary = []
                
                # Add timestamp
                summary.append(f"Window Summary at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                summary.append(f"Total Monitors: {data['summary']['total_monitors']}")
                summary.append(f"Total Windows: {data['summary']['total_windows']}")
                summary.append(f"Total Applications: {data['summary']['total_apps']}")
                summary.append("")
                
                # Add monitor details
                for monitor_id, monitor_data in data["monitors"].items():
                    summary.append(f"=== {monitor_id.upper()} ===")
                    summary.append(f"Device: {monitor_data['device']}")
                    summary.append(f"Resolution: {monitor_data['width']}x{monitor_data['height']}")
                    summary.append(f"Primary: {'Yes' if monitor_data['primary'] else 'No'}")
                    summary.append(f"Windows: {monitor_data['window_count']}")
                    summary.append("")
                    
                    # Add application details
                    for app_name, app_data in monitor_data["applications"].items():
                        summary.append(f"  {app_name} ({app_data['window_count']} windows)")
                        for window_id, window in app_data["windows"].items():
                            state = "MINIMIZED" if window["minimized"] else "VISIBLE"
                            summary.append(f"    - {window['title']} ({state})")
                        summary.append("")
                
                return {'success': True, 'message': '\n'.join(summary)}
            elif command == 'refresh_windows':
                # Refresh the window list
                data = self.wm.get_structured_windows()
                # Update short ID lookup
                self.refresh_window_short_id_lookup()
                return {'success': True, 'message': f"Refreshed {data['summary']['total_windows']} windows"}
            else:
                window_id = params.get('window_id')
                if not window_id:
                    return {'error': 'Window ID required'}

                # NEW: Support short window ID
                if window_id not in self.window_short_id_lookup.values():
                    # If not a full ID, try to resolve as short ID
                    if window_id in self.window_short_id_lookup:
                        window_id = self.window_short_id_lookup[window_id]
                    else:
                        return {'error': f"Window ID '{window_id}' not found (full or short ID)"}

                # Extract HWND from composite window_id
                hwnd = int(window_id.split('_')[0])

                logger.info(f"Executing _execute_window_command: {command} {params} {window_id}")
                if command == 'maximize':
                    success, message = self.wm.maximize_window(hwnd)
                    logger.info(f"Executing _execute_window_command: {command} {params} {window_id} {success} {message}")
                elif command == 'minimize':
                    success, message = self.wm.minimize_window(hwnd)
                elif command == 'close':
                    success, message = self.wm.close_window(hwnd)
                elif command == 'resize':
                    success, message = self.wm.resize_window(hwnd, params['width'], params['height'])
                elif command == 'move':
                    success, message = self.wm.move_window(hwnd, params['x'], params['y'])
                elif command == 'screen':
                    success, message = self.wm.move_window_to_screen_position(hwnd, params['screen'], params['x'], params['y'])
                elif command == 'monitor':
                    success, message = self.wm.move_window_to_monitor(hwnd, params['monitor'])
                elif command == 'introspect':
                    success, message = self.wm.introspect_window(hwnd)
                elif command == 'tree':
                    success, message = self.wm.get_window_hierarchy_tree(hwnd)
                else:
                    return {'error': f'Unknown window command: {command}'}

            return {'success': success, 'message': message}
        except Exception as e:
            return {'error': str(e)}

    async def _execute_mouse_command(self, command: str, params: Dict) -> Dict:
        """Execute mouse-related command"""
        logger.info(f"Executing _execute_mouse_command: {command} {params}")
        if command == 'click':
            button = params.get('button', 'left')
            x = int(params.get('x')) if params.get('x') is not None else None
            y = int(params.get('y')) if params.get('y') is not None else None
            success, message = self.wm.send_mouse_click(button, x, y)
        elif command == 'doubleclick':
            success, message = self.wm.send_mouse_double_click(params.get('button', 'left'), params.get('x'), params.get('y'))
        elif command == 'longclick':
            success, message = self.wm.send_mouse_long_click(params.get('button', 'left'), params.get('duration', 1.0), params.get('x'), params.get('y'))
        elif command == 'scroll':
            direction = params.get('direction', 'up')
            amount = int(params.get('amount', 3))
            x = int(params.get('x')) if params.get('x') is not None else None
            y = int(params.get('y')) if params.get('y') is not None else None
            success, message = self.wm.send_mouse_scroll(direction, amount, x, y)
        elif command == 'drag':
            success, message = self.wm.send_mouse_drag(params['start_x'], params['start_y'], params['end_x'], params['end_y'], params.get('button', 'left'), params.get('duration', 0.5))
        else:
            return {'error': f'Unknown mouse command: {command}'}

        return {'success': success, 'message': message}

    async def _execute_keyboard_command(self, command: str, params: Dict) -> Dict:
        """Execute keyboard-related command"""
        logger.info(f"Executing _execute_keyboard_command: {command} {params}")
        if command == 'send':
            success, message = self.wm.send_key_combination(params['keys'])
        elif command == 'type':
            success, message = self.wm.send_text(params['text'])
        else:
            return {'error': f'Unknown keyboard command: {command}'}

        return {'success': success, 'message': message}

    async def _execute_system_command(self, command: str, params: Dict) -> Dict:
        """Execute system-related command"""
        logger.info(f"Executing _execute_system_command: {command} {params}")
        try:
            if command == 'launch':
                # Convert types robustly
                app_name = params.get('app_name')
                screen_id = int(params.get('screen_id', 1))
                fullscreen = params.get('fullscreen', False)
                if isinstance(fullscreen, str):
                    fullscreen = fullscreen.lower() in ('true', '1', 'yes')
                success, message = self.wm.launch_application(app_name, screen_id, fullscreen)
            elif command == 'msgbox':
                title = params.get('title', '')
                message_ = params.get('message', '')
                x = params.get('x')
                y = params.get('y')
                x = int(x) if x is not None else None
                y = int(y) if y is not None else None
                success, message = self.wm.show_message_box(title, message_, x, y)
            elif command == 'computer':
                success, message = self.wm.get_computer_name()
            elif command == 'user':
                success, message = self.wm.get_user_name()
            elif command == 'keys':
                success, message = self.wm.get_virtual_key_codes()
            elif command == 'screenshot':
                try:
                    path = take_screenshot()
                    return {'success': True, 'message': f"Screenshot saved: {path}", 'filepath': path}
                except Exception as e:
                    return {'success': False, 'message': str(e)}
            elif command == 'hover' or command == 'detect':
                try:
                    result = self.wm.get_element_under_cursor()
                    return {'success': True, 'message': f"Element under cursor: {result}", 'result': result}
                except Exception as e:
                    return {'success': False, 'message': str(e)}
            elif command == 'inspect':
                try:
                    result = self.wm.get_element_under_cursor()
                    return {'success': True, 'message': f"Full window analysis: {result}", 'result': result}
                except Exception as e:
                    return {'success': False, 'message': str(e)}
            elif command == 'cursor':
                x = params.get('x')
                y = params.get('y')
                if x is not None and y is not None:
                    try:
                        x = int(x)
                        y = int(y)
                        success, message = self.wm.set_cursor_position(x, y)
                        return {'success': success, 'message': message}
                    except Exception as e:
                        return {'success': False, 'message': str(e)}
                else:
                    try:
                        success, message, pos = self.wm.get_cursor_position()
                        if success:
                            return {'success': True, 'message': f"Cursor position: {pos}", 'position': pos}
                        else:
                            return {'success': False, 'message': message}
                    except Exception as e:
                        return {'success': False, 'message': str(e)}
            else:
                return {'error': f'Unknown system command: {command}'}

            return {'success': success, 'message': message}
        except Exception as e:
            return {'error': str(e)}

    async def _send_event(self, response: web.StreamResponse, event_type: str, data: Dict):
        """Send SSE event to a client"""
        try:
            await response.write(f"event: {event_type}\n".encode())
            await response.write(f"data: {json.dumps(data)}\n\n".encode())
        except Exception as e:
            print(f"Error sending event: {e}")
            self.clients.discard(response)

    async def _broadcast_event(self, event_type: str, data: Dict):
        """Broadcast SSE event to all clients"""
        # Create a copy of the clients set to avoid modification during iteration
        clients_to_process = self.clients.copy()
        disconnected_clients = set()
        
        for client in clients_to_process:
            try:
                await self._send_event(client, event_type, data)
            except Exception as e:
                print(f"Error broadcasting to client: {e}")
                disconnected_clients.add(client)
        
        # Clean up disconnected clients
        for client in disconnected_clients:
            self.clients.discard(client)
            try:
                await client.write_eof()
            except Exception as e:
                print(f"Error closing disconnected client: {e}")

    async def shutdown(self):
        """Gracefully shutdown the server"""
        self._running = False
        
        # Close all client connections
        for client in self.clients:
            try:
                await client.write_eof()
            except Exception as e:
                print(f"Error closing client connection: {e}")
        
        self.clients.clear()

    async def print_server_commands(self):
        """Fetch and print the available commands from the server."""
        tools = await self.handle_tools(None)
        print("\n=== MCP Server Commands ===")
        for section, commands in tools.json():
            if not commands:
                continue
            section_title = {
                "window_commands": "📋 Window Commands",
                "mouse_commands": "🖱️  Mouse Commands",
                "keyboard_commands": "⌨️  Keyboard Commands",
                "system_commands": "💻 System Commands"
            }.get(section, section)
            print(f"\n{section_title}:")
            for cmd, info in commands.items():
                params = ', '.join(f"{k}: {v}" for k, v in info.get('params', {}).items())
                print(f"  • {cmd}: {info['description']}")
                if params:
                    print(f"    Parameters: {params}")
        

async def main():
    server = MCPServer()
    app = web.Application()
    
    # Routes
    app.router.add_get('/sse', server.handle_sse)
    app.router.add_post('/command', server.handle_command)
    app.router.add_get('/tools', server.handle_tools)
    app.router.add_get('/history', server.handle_history)
    
    # Start server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', 8080)
    await site.start()
    
    print("MCP Server running at http://localhost:8080")
    
    try:
        # Keep server running
        while server._running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        await server.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped by user")