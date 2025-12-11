#!/usr/bin/env python3
"""
Диагностический скрипт для проверки активных процессов и их состояния
"""
import psutil
import sys
import os

def check_bot_processes():
    """Проверяет активные процессы бота"""
    bot_processes = []
    
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'status', 'create_time', 'num_threads']):
        try:
            cmdline = ' '.join(proc.info['cmdline']) if proc.info['cmdline'] else ''
            
            # Ищем процессы, связанные с ботом
            if 'python' in proc.info['name'].lower():
                if 'main.py' in cmdline and 'hype' in cmdline.lower():
                    bot_processes.append({
                        'pid': proc.info['pid'],
                        'name': proc.info['name'],
                        'cmdline': cmdline[:200],
                        'status': proc.info['status'],
                        'create_time': proc.info['create_time'],
                        'num_threads': proc.info['num_threads'],
                        'cpu_percent': proc.cpu_percent(interval=0.1),
                        'memory_mb': proc.memory_info().rss / 1024 / 1024
                    })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    
    return bot_processes

def check_active_connections(pid):
    """Проверяет активные сетевые соединения процесса"""
    try:
        proc = psutil.Process(pid)
        connections = proc.connections()
        return connections
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []

def main():
    print("=" * 60)
    print("Bot Process Diagnostics")
    print("=" * 60)
    
    processes = check_bot_processes()
    
    if not processes:
        print("✅ No active bot processes found")
        return
    
    print(f"\n⚠️ Found {len(processes)} active bot process(es):\n")
    
    for proc in processes:
        print(f"PID: {proc['pid']}")
        print(f"  Status: {proc['status']}")
        print(f"  Threads: {proc['num_threads']}")
        print(f"  CPU: {proc['cpu_percent']:.1f}%")
        print(f"  Memory: {proc['memory_mb']:.1f} MB")
        print(f"  Cmd: {proc['cmdline'][:100]}...")
        
        # Проверяем активные соединения
        connections = check_active_connections(proc['pid'])
        if connections:
            print(f"  Active connections: {len(connections)}")
            for conn in connections[:5]:  # Показываем первые 5
                print(f"    - {conn.status}: {conn.raddr if conn.raddr else 'N/A'}")
        else:
            print(f"  Active connections: 0")
        
        print()
    
    print("=" * 60)
    print("Note: If bot is stopped, pending tasks no longer exist")
    print("      (they are cancelled when event loop closes)")
    print("=" * 60)

if __name__ == '__main__':
    main()