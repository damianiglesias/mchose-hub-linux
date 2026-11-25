#!/usr/bin/env python3
"""Extractor de formato ASAR custom con header JSON inline y bloques comprimidos xz.

Formato MHUB ASAR:
- Magic header 4 bytes (0x04, 0x00, 0x00, 0x00) en inicio del archivo  
- Header JSON completo desde offset ~16 a ~578KB con estructura: {"files": {...}, "resources": {...}}
- Cada entry tiene: size, integrity {algorithm, hash}, blockSize, blocks[], offset

Los bloques están comprimidos con xz/lzma y los hashes en el header son SHA256 de cada bloque.
"""

import json
import subprocess
import os


def extract_files_from_header(asar_path):
    """Extraer lista detallada de archivos del header JSON personalizado."""
    
    with open(asar_path, 'rb') as f:
        file_data = f.read()
    
    print(f"=== ANÁLISIS DE FORMATO ASAR CUSTOM MHUB ===")
    print(f"Tamaño total: {len(file_data):.2f} MB ({len(file_data)} bytes)")
    print()
    
    # Verificar magic header (4 bytes iniciales)
    if len(file_data) >= 4:
        magic = file_data[:4]
        print(f"Magic header: {magic.hex()}")
        
        # Buscar inicio del JSON {"files":...}  
        json_start = file_data.find(b'{"files"', 0)
        resources_end = file_data.rfind(b'"resources"}', 0, len(file_data)) + len(b'"resources"}')
        
        print(f"JSON header inicia en offset: {json_start}")
        if resources_end > 0 and json_start >= 0:
            json_header_length = resources_end - json_start
            print(f"Header JSON termina aproximadamente en offset: {resources_end} ({json_header_length / (1024*1024):.3f} MB)")
    
    print()
    print("=" * 60)
    print("=== EXTRAER LISTA DETALLADA DE ARCHIVOS DEL HEADER ===")
    print("=" * 60)
    
    # Extraer y parsear el header JSON completo  
    try:
        if json_start >= 0 and resources_end > json_start:
            json_str = file_data[json_start:resources_end].decode('utf-8')
            
            headers_json = json.loads(json_str)
            
            print(f"\n=== SECCIÓN 'FILES' (Data Files - app.asar contiene): ===")
            if "files" in headers_json and isinstance(headers_json["files"], dict):
                for i, (filename, fileinfo) in enumerate(sorted(headers_json["files"].items()), 1):
                    try:
                        blocks = fileinfo.get("blocks", [])
                        
                        # Calcular tamaño total del archivo desde los hashes de bloques  
                        block_hashes_str = "".join(blocks) if isinstance(blocks, list) else ""
                        block_size = fileinfo.get("blockSize", 4194304)
                        
                        print(f"\n{i}. {filename}")
                        print(f"   Original size: {fileinfo.get('size', 'N/A')} bytes")
                        if blocks and len(blocks) > 0:
                            num_blocks = len(blocks)
                            total_compressed_bytes = len(block_hashes_str.encode())
                            uncompressed_estimate = fileinfo.get("size", block_size * num_blocks / 3.5)  # xz ratio ~2-4x compression
                            
                            print(f"   Block size: {block_size:,} bytes ({block_size/1024:.1f} KB)")
                            if total_compressed_bytes > 0:
                                avg_block = total_compressed_bytes / num_blocks
                                print(f"   Bloques comprimidos: {num_blocks}")
                                
                        # Identificar archivos de interés para protocolo HID  
                        filename_lower = filename.lower()
                        
                        if "hidreport.js" in filename_lower or "setdpi" in filename_lower:
                            marker = "[INTERÉS HID]"
                        elif ".js" == filename[-3:] and ("renderer" not in filename.lower()):
                            marker = f"[JS - {filename}]"  
                            
                    except Exception as e:
                        print(f"\n{i}. {filename}")
                        print(f"   Error procesando: {e}")
                
                # Mostrar resumen de archivos JS (donde está el código HID)
                js_files = [f for f in headers_json["files"].keys() if f.endswith('.js')]
                print(f"\n=== ARCHIVOS .JS ENCONTRADOS ({len(js_files)}): ===")  
                for js_file in sorted(js_files)[:20]:  # Mostrar primeros 20
                    fileinfo = headers_json["files"][js_file]
                    size_str = f"({fileinfo.get('size', 'N/A')} bytes)" if isinstance(fileinfo, dict) else ""
                    print(f"   {js_file}{size_str}")
            
            print()  
            print("\n=== SECCIÓN 'RESOURCES' (Assets): ===")
            if "resources" in headers_json and isinstance(headers_json["resources"], dict):
                for i, (filename, fileinfo) in enumerate(sorted(headers_json["resources"].items()), 1):
                    try:
                        blocks = fileinfo.get("blocks", [])
                        
                        block_hashes_str = "".join(blocks) if isinstance(blocks, list) else ""  
                        block_size = fileinfo.get("blockSize", 4194304)
                        
                        print(f"\n{i}. {filename}")
                        print(f"   Original size: {fileinfo.get('size', 'N/A')} bytes")  
                        if blocks and len(blocks) > 0:
                            num_blocks = len(blocks)
                            
                    except Exception as e:
                        pass
                
    except json.JSONDecodeError as je:
        print(f"\n[ERROR] Decodificando header JSON:")
        print(f"{je}")


def find_hid_related_files(asar_path):
    """Buscar archivos relacionados con HID usando grep dentro del ASAR."""
    
    with open(asar_path, 'rb') as f:
        file_data = f.read()

    json_start = file_data.find(b'{"files"', 0)  
    resources_end_pos = file_data.rfind(b'"resources"}', 0, len(file_data)) + len(b'"resources"}')
    
    if json_start < 0 or resources_end_pos <= json_start:
        return []

    try:
        headers_json = json.loads(file_data[json_start:resources_end_pos].decode('utf-8'))
        files_section = headers_json.get("files", {})

        # Archivos de interés para protocolo HID  
        hid_patterns = ["hidreport.js", "setdpi", "featureReport", "mchose"]

        found_files = []
        for filename in sorted(files_section.keys()):
            if any(pattern.lower() in filename.lower() for pattern in hid_patterns):
                fileinfo = files_section[filename]
                size_str = f" ({fileinfo.get('size', 'N/A')} bytes)"  

                found_files.append(f"{filename}{size_str}")

        return found_files

    except Exception as e:
        print(f"Error buscando archivos HID: {e}")
        return []


def main():
    import sys
    
    if len(sys.argv) < 2:
        asar_path = '/home/damian/Downloads/MHUB/resources/app.asar'  
    else:
        asar_path = sys.argv[1]
    
    print(f"\nAnalizando ASAR desde: {asar_path}")
    extract_files_from_header(asar_path)
    
    # Buscar archivos relacionados con HID  
    hid_files = find_hid_related_files(asar_path)
    
    if hid_files:
        print("\n" + "=" * 60)
        print("ARCHIVOS DE INTERÉS PARA PROTOCOLO HID:")
        for f in hid_files[:15]:
            print(f"   {f}")


if __name__ == '__main__':
    main()
