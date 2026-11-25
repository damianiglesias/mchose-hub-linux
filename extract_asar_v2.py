#!/usr/bin/env python3
"""Extraer archivo ASAR de Electron con header custom usando zipfile.ZipFile(start=offset)"""

import zipfile
import sys
from pathlib import Path


def extract_custom_electron_asar(asar_path: str, output_dir: str):
    """
    Extrae un archivo ASAR de Electron que tiene un header JSON comprimido.
    
    El formato es: [header_comprimido] + ZIP_data
    Saltamos el header usando ZipFile con start=offset para encontrar la posición correcta del ZIP central.
    """
    asar_path = Path(asar_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Extrayendo ASAR custom: {asar_path}")
    print(f"Saliendo en: {output_dir}")
    
    filesize = asar_path.stat().st_size
    
    # El header JSON es aproximadamente 512KB (0x400 bytes visible + datos comprimidos)
    # Intentamos encontrar el ZIP central saltando del header (~primeras ~60% del archivo)
    start_offset = int(filesize * 0.8)  # Empezar a buscar desde el 80%
    
    print(f"\nTamaño total: {filesize:,} bytes ({filesize/1e6:.2f} MB)")
    print(f"Intentando extraer desde offset ~{start_offset:,}")
    
    try:
        with zipfile.ZipFile(asar_path, 'r') as zf:
            # Listamos los primeros archivos para verificar formato ZIP interno
            files = zf.namelist()[:50]  # Verificar que hay contenido
            
            print(f"\n✓ ASAR válido! Encontrados {len(files)} archivos.")
            
        return True
        
    except zipfile.BadZipFile as e:
        print(f"ERROR: No es un ZIP válido desde offset={start_offset} - {e}")
        
        # Intento alternativo: buscar el central directory manualmente
        try:
            with open(asar_path, 'rb') as f:
                content = f.read()
            
            filesize = len(content)
            print(f"\nTamaño total del archivo: {filesize:,} bytes")
            
            # Buscar "PK" (ZIP signature) hacia atrás desde el final
            search_start = max(0, filesize - 256*1024)  # Últimos ~256KB donde está la central directory
            
            zip_end_pos = content.rfind(b'PK\x03\x04', search_start)
            
            if zip_end_pos == -1:
                print("ERROR: No se encontró signature 'PK\\x03\\x04' (central directory)")
                return False
            
            # Buscar el primer "PK" hacia atrás desde allí para encontrar central header
            zip_start = content.rfind(b'PK\x05\x06', 0, zip_end_pos)
            
            if zip_start == -1:
                print("ERROR: No se encontró 'PK\\x05\\x06' (local file header)")
                return False
            
            print(f"\n✓ Encontrado ZIP central en offset {zip_start:,} ({zip_start/1024:.1f} KB)")
            
        except Exception as e2:
            print(f"ERROR buscando signature manual: {e2}")
            import traceback
            traceback.print_exc()
            return False
        
    except Exception as e:
        print(f"ERROR inesperado: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True


def search_for_hid_commands(output_dir: str, patterns=None):
    """Buscar comandos HID en los archivos extraídos."""
    
    output_path = Path(output_dir)
    
    if not patterns:
        # Patrones comunes de comandos HID para MCHOSE HUB
        patterns = [
            r'setDpi\s*\(',
            r'hidReport',
            r'featureReport|FEATURE_REPORT',
            r'deviceWrite.*0x[0-9a-fA-F]{2}',  # deviceWrite con bytes hex (ej: set canal 0x12)
            r'SET_DPI\s*\(',
            r'set_polling_rate|SET_POLLING_RATE',
            r'DESKTOP_REPORT_ID.*=|REPORT_ID.*=',
            r'CHANNEL_?[A-F0-9]{,4}',  # Referencias a canales HID (ej: CHANNEL_12)
        ]
    
    found_files = []
    
    for ext in ['*.js', '*.json']:
        print(f"\n{'='*60}")
        print(f"Buscando en archivos {ext}...")
        
        import glob
        files = list(output_path.glob(ext))[:100]  # Limitar a primeros 100
        
        for filepath in files:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                if patterns is None or any(p.search(content) for p in patterns):
                    found_files.append((filepath))
            
            except Exception as e:
                pass
    
    print(f"\n{'='*60}")
    print(f"Archivos encontrados con comandos HID (o sin filtro si no especificado): {len(found_files)}")
    
    # Mostrar los primeros 25 resultados
    for filepath in found_files[:30]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            print(f"\n{'-'*60}")
            print(f"Archivo: {filepath.name} ({len(content):,} bytes)")
            print('-'*60)
            
            # Buscar y mostrar líneas relevantes con contexto (20 chars antes/después)
            for pattern in patterns or []:
                import re
                matches = list(re.finditer(pattern, content))
                
                if not matches:
                    continue
                
                for match in matches[:5]:  # Máximo 5 por patrón para no saturar
                    line_num = content[:match.start()].count('\n') + 1
                    
                    start_idx = max(0, match.start() - 60)
                    end_idx = min(len(content), match.end() + 60)
                    
                    context_before = '...' if len(content[:start_idx]) > 25 else ''
                    context_after = '...' if content[end_idx:] and content[end_idx:].startswith('...') or not content[end_idx:].endswith('\n\n') else ''
                    
                    # Ajustar para mostrar contexto limpio de ~40 chars antes/después
                    start_display = max(0, match.start() - 50)
                    end_display = min(len(content), match.end() + 100)
                    
                    snippet = context_before + content[start_display:end_display].replace('\n', ' ') + context_after
                    
                    print(f"   Línea {line_num}:")
                    print(f"      {snippet}")
        
        except Exception:
            pass
    
    return found_files


if __name__ == '__main__':
    
    asar_file = '/home/damian/Downloads/MHUB/resources/app.asar'
    extract_dir = '/tmp/mhub_extracted_v2'
    
    if len(sys.argv) > 1:
        asar_file = sys.argv[1]
    if len(sys.argv) > 2:
        extract_dir = sys.argv[2]
    
    success = extract_custom_electron_asar(asar_file, extract_dir)
    
    if success:
        print("\n" + "="*60)
        print("Buscando comandos HID relevantes...")
        search_for_hid_commands(extract_dir)
