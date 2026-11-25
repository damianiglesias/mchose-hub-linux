#!/usr/bin/env python3
"""Extractor de formato ASAR custom con header JSON y bloques comprimidos."""

import json


def extract_files_from_header(asar_path):
    """Extraer lista de archivos del header JSON personalizado."""
    
    with open(asar_path, 'rb') as f:
        file_data = f.read()
    
    print(f"=== ANÁLISIS DE FORMATO ASAR CUSTOM ===")
    print(f"Tamaño total: {len(file_data):.2f} MB ({len(file_data)} bytes)")
    print()
    
    # El header JSON custom se encuentra en los últimos ~564KB del archivo (según análisis hex)
    estimated_header_size = 564327
    
    if len(file_data) > estimated_header_size:
        json_start_pos = len(file_data) - estimated_header_size
        
        print(f"Header JSON aproximado está en offset {json_start_pos}")
        
        # Extraer el header desde esa posición (incluyendo posible padding binario antes de él)
        try:
            # El formato custom tiene metadata JSON con estructura:
            # {"files": {...}, "resources": {...}} donde cada entry tiene blockSize, blocks[], offset
            
            print(f"\n=== EXTRAER LISTA DE ARCHIVOS DEL HEADER ===")
            
            json_start = file_data.find(b'{"files"', max(0, json_start_pos - 1024))
            
            if json_start == -1:
                # Buscar por patrón conocido del header custom
                files_marker = file_data.find(b'"packa', len(file_data) - estimated_header_size * 2)
                
        except Exception as e:
            print(f"Error buscando inicio JSON: {e}")


def main():
    import sys
    
    if len(sys.argv) < 2:
        asar_path = '/home/damian/Downloads/MHUB/resources/app.asar'
    else:
        asar_path = sys.argv[1]
    
    extract_files_from_header(asar_path)


if __name__ == '__main__':
    main()
