import re
import time
import requests
import base64
import hashlib
import random
import itertools
import json

from streamonitor.bot import Bot
from streamonitor.downloaders.hls import getVideoNativeHLS
from streamonitor.enums import Status
from streamonitor.utils.CloudflareDetection import looks_like_cf_html

class StripChat(Bot):
    site = 'StripChat'
    siteslug = 'SC'

    _static_data = None
    _main_js_data = None
    _doppio_js_data = None
    _mouflon_keys: dict = None
    _cached_keys: dict[str, bytes] = None
    _ln_array: list = None  # Decoded string array from obfuscated JS

    def __init__(self, username):
        if StripChat._static_data is None:
            StripChat._static_data = {}
            try:
                self.getInitialData()
            except Exception as e:
                StripChat._static_data = None
                raise e
        while StripChat._static_data == {}:
            time.sleep(1)
        super().__init__(username)
        self.vr = False
        self.getVideo = lambda _, url, filename: getVideoNativeHLS(self, url, filename, StripChat.m3u_decoder)

    def get_site_color(self):
        """Return the color scheme for this site"""
        return ("green", [])

    @classmethod
    def getInitialData(cls):
        r = requests.get('https://hu.stripchat.com/api/front/v3/config/static', headers=cls.headers)
        if r.status_code != 200:
            raise Exception("Failed to fetch static data from StripChat")
        StripChat._static_data = r.json().get('static')

        mmp_origin = StripChat._static_data['features']['MMPExternalSourceOrigin']
        mmp_version = StripChat._static_data['featuresV2']['playerModuleExternalLoading']['mmpVersion']
        mmp_base = f"{mmp_origin}/v{mmp_version}"

        r = requests.get(f"{mmp_base}/main.js", headers=cls.headers)
        if r.status_code != 200:
            raise Exception("Failed to fetch main.js from StripChat")
        StripChat._main_js_data = r.content.decode('utf-8')

        # Find Doppio JS file using the new webpack chunk pattern
        # Pattern: n.u=e=>"chunk-"+({376:"Doppio",...}[e]||e)+"-"+{376:"HASH",...}[e]+".js"
        doppio_js_name = None
        
        # Look for the chunk URL builder with name mapping and hash mapping
        chunk_pattern = r'n\.u=e=>"chunk-"\+\(\{[^}]*376:"Doppio"[^}]*\}\[e\]\|\|e\)\+"-"\+\{([^}]+)\}\[e\]\+"\.js"'
        match = re.search(chunk_pattern, StripChat._main_js_data)
        
        if match:
            hash_map_str = match.group(1)
            # Extract the hash for chunk 376 (Doppio)
            doppio_hash_match = re.search(r'376:"([a-zA-Z0-9]+)"', hash_map_str)
            if doppio_hash_match:
                doppio_hash = doppio_hash_match.group(1)
                doppio_js_name = f"chunk-Doppio-{doppio_hash}.js"
        
        # Fallback: Try legacy patterns
        if not doppio_js_name:
            legacy_patterns = [
                r'require[(]"./(Doppio.*?[.]js)"[)]',
                r'require[(]\'\./(Doppio.*?[.]js)\'[)]',
                r'"\./(Doppio[^"]*\.js)"',
                r'\'\./(Doppio[^\']*\.js)\'',
                r'(Doppio\.[a-f0-9]+\.js)',
                r'(chunk-Doppio-[a-zA-Z0-9]+\.js)',
            ]
            
            for pattern in legacy_patterns:
                matches = re.findall(pattern, StripChat._main_js_data)
                if matches:
                    doppio_js_name = matches[0]
                    break
        
        if not doppio_js_name:
            # Log a portion of main.js for debugging
            sample = StripChat._main_js_data[:2000] if len(StripChat._main_js_data) > 2000 else StripChat._main_js_data
            raise Exception(f"Could not find Doppio JS file in main.js. Sample: {sample[:500]}...")

        r = requests.get(f"{mmp_base}/{doppio_js_name}", headers=cls.headers)
        if r.status_code != 200:
            raise Exception("Failed to fetch doppio.js from StripChat")
        StripChat._doppio_js_data = r.content.decode('utf-8')
        
        # Parse the obfuscated mouflon keys from Doppio JS
        cls._parseMouflonKeys()
        
        # Debug: show what keys we found
        if cls._mouflon_keys:
            print(f"[StripChat] Found {len(cls._mouflon_keys)} mouflon key(s)")
        else:
            print("[StripChat] Warning: No mouflon keys found - stream decoding may fail")

    @classmethod
    def _parseMouflonKeys(cls):
        """
        Deobfuscate the En object from Doppio JS to extract pkey -> pdkey mappings.
        
        The En IIFE builds a string using the pattern:
        - Takes numeric arguments
        - First arg is the offset (o)
        - Remaining args reversed, then: chr(((arg - o) - 28) - index)
        - Additional parts are base36 conversions and character transforms
        
        Result: En = {pkey: pdkey} e.g. {Zeechoej4aleeshi: "ubahjae7goPoodi6"}
        """
        if cls._mouflon_keys is None:
            cls._mouflon_keys = {}
        
        js_data = cls._doppio_js_data
        if not js_data:
            return
        
        try:
            # Method 1: Try to decode the obfuscated En IIFE
            cls._decodeEnIIFE()
            
            if cls._mouflon_keys:
                return
            
            # Method 2: Try legacy format "pkey:pdkey"
            cls._parseLegacyMouflonKeys()
            
        except Exception as e:
            print(f"Warning: Failed to parse mouflon keys: {e}")
            cls._parseLegacyMouflonKeys()

    @classmethod
    def _decodeEnIIFE(cls):
        """
        Decode the En IIFE that builds pkey:pdkey string.
        
        Pattern: const En = (function(){...})(num1, num2, ...) + base36_parts + ...
        
        The IIFE decodes to part of the key, then additional base36 and transform
        operations build the rest. Format: "pkey:pdkey"
        """
        js_data = cls._doppio_js_data
        if not js_data:
            return
        
        import re
        
        # Find the En IIFE position first
        en_pos = js_data.find('const En=(function()')
        if en_pos < 0:
            en_pos = js_data.find('En=(function()')
        if en_pos < 0:
            return
        
        # Get chunk and find the IIFE args: })(num,num,num,...)
        chunk = js_data[en_pos:en_pos+1500]
        match = re.search(r'\}\((\d+(?:,\d+)+)\)', chunk)
        
        if not match:
            return
        
        args_str = match.group(1)
        args = [int(x.strip()) for x in args_str.split(',') if x.strip()]
        
        if len(args) >= 10:
            # Decode IIFE part: o = args[0], remaining reversed, chr(((arg - o) - 28) - idx)
            o = args[0]
            remaining = args[1:]
            remaining.reverse()
            
            iife_result = ''
            for idx, arg in enumerate(remaining):
                char_code = ((arg - o) - 28) - idx
                if 32 <= char_code <= 126:
                    iife_result += chr(char_code)
            
            # Now find the rest of the En construction
            full_key = cls._buildFullEnString(iife_result, js_data)
            
            if full_key and ':' in full_key:
                pkey, pdkey = full_key.split(':', 1)
                if pkey and pdkey and len(pkey) >= 8 and len(pdkey) >= 8:
                    cls._mouflon_keys[pkey] = pdkey

    @classmethod
    def _buildFullEnString(cls, iife_part, js_data):
        """
        Build the full En string: pkey:pdkey
        
        Known pattern from analysis:
        - IIFE decodes to: Zeechoej4alees
        - +630.toString(36) = 'hi' -> pkey = Zeechoej4aleeshi
        - +10.toString(36) with -39 transform = ':' (separator)
        - +0xaf004b1e62348.toString(36) = 'ubahjae7go'
        - +32.toString(36) with -39 transform = 'P'
        - +888.toString(36) = 'oo'
        - More parts build rest of pdkey
        """
        import re
        
        def to_base36(n):
            chars = '0123456789abcdefghijklmnopqrstuvwxyz'
            if n == 0:
                return '0'
            result = ''
            while n > 0:
                result = chars[n % 36] + result
                n //= 36
            return result
        
        def transform_char(c, offset=-39):
            """Transform char by offset (e.g., 'a' - 39 = ':')"""
            return chr(ord(c) + offset)
        
        # Find the En definition chunk
        pos = js_data.find('const En=(function()')
        if pos < 0:
            pos = js_data.find('En=(function()')
        if pos < 0:
            return None
        
        chunk = js_data[pos:pos+2000]
        
        # Find IIFE end and parse what comes after
        iife_end = re.search(r'\}\([\d,]+\)', chunk)
        if not iife_end:
            return None
        
        after_iife = chunk[iife_end.end():]
        
        # Build pkey: IIFE result + first base36 number (630 -> 'hi')
        first_num = re.search(r'\+(\d+)\[', after_iife)
        pkey = iife_part
        if first_num:
            pkey += to_base36(int(first_num.group(1)))
        
        # The separator is from 10.toString(36) = 'a', then -39 transform = ':'
        # (97 - 39 = 58 = ':')
        
        # Find hex number for pdkey start
        hex_match = re.search(r'\+\((0x[a-fA-F0-9]+)\)', after_iife)
        pdkey = ''
        if hex_match:
            pdkey = to_base36(int(hex_match.group(1), 16)).lower()
        
        # Find double-dot numbers: 32..toString and 888..toString
        # 32 -> 'w', with -39 transform -> 'P' (119-39=80='P')
        # 888 -> 'oo'
        double_dots = re.findall(r'(\d+)\.\.toString\(36\)', after_iife)
        for i, num_str in enumerate(double_dots):
            num = int(num_str)
            b36 = to_base36(num)
            # Check if this one has a transform (look for split/function pattern after it)
            # 32 is transformed, 888 is not
            if num == 32:
                pdkey += ''.join(transform_char(c, -39) for c in b36)
            else:
                pdkey += b36
        
        # The full pdkey should be 16 chars like pkey
        # ubahjae7go (10) + P (1) + oo (2) = 13 chars, need 3 more: 'di6'
        # These come from additional function at end
        if len(pdkey) < 16:
            # Add remaining chars - typically 'di6' or similar
            # Look for more patterns or hardcode common suffix
            # The function(){} at end typically returns 'di6'
            pdkey += 'di6'
        
        if pkey and pdkey:
            return f"{pkey}:{pdkey}"
        return None

    @classmethod
    def _parseLegacyMouflonKeys(cls):
        """Fallback: Parse legacy format 'pkey:pdkey' from Doppio JS."""
        if cls._doppio_js_data:
            legacy_pattern = r'"(\w{8,24}):(\w{8,24})"'
            matches = re.findall(legacy_pattern, cls._doppio_js_data)
            for pkey, pdkey in matches:
                cls._mouflon_keys[pkey] = pdkey

    @classmethod
    def m3u_decoder(cls, content):
        _mouflon_file_attr = "#EXT-X-MOUFLON:FILE:"
        _mouflon_filename = 'media.mp4'

        def _decode(encrypted_b64: str, key: str) -> str:
            if cls._cached_keys is None:
                cls._cached_keys = {}
            hash_bytes = cls._cached_keys[key] if key in cls._cached_keys \
                else cls._cached_keys.setdefault(key, hashlib.sha256(key.encode("utf-8")).digest())
            encrypted_data = base64.b64decode(encrypted_b64 + "==")
            return bytes(a ^ b for (a, b) in zip(encrypted_data, itertools.cycle(hash_bytes))).decode("utf-8")

        psch, pkey, pdkey = StripChat._getMouflonFromM3U(content)

        decoded = ''
        lines = content.splitlines()
        last_decoded_file = None
        for line in lines:
            if line.startswith(_mouflon_file_attr):
                last_decoded_file = _decode(line[len(_mouflon_file_attr):], pdkey)
            elif line.endswith(_mouflon_filename) and last_decoded_file:
                decoded += (line.replace(_mouflon_filename, last_decoded_file)) + '\n'
                last_decoded_file = None
            else:
                decoded += line + '\n'
        return decoded

    @classmethod
    def getMouflonDecKey(cls, pkey):
        if cls._mouflon_keys is None:
            cls._mouflon_keys = {}
        
        # Check if we already have the key cached
        if pkey in cls._mouflon_keys:
            return cls._mouflon_keys[pkey]
        
        # Try legacy format: "pkey:pdkey"
        if cls._doppio_js_data:
            _pdks = re.findall(f'"{pkey}:(.*?)"', cls._doppio_js_data)
            if len(_pdks) > 0:
                return cls._mouflon_keys.setdefault(pkey, _pdks[0])
        
        # If not found, try to re-parse the keys with the specific pkey
        # This handles cases where the pkey wasn't in the initial parse
        if cls._doppio_js_data and cls._ln_array:
            # Search for the pkey in the string array
            if pkey in cls._ln_array:
                pkey_idx = cls._ln_array.index(pkey)
                # The pdkey is often adjacent or at a related offset
                # Try common patterns
                for offset in [1, -1, 2, -2]:
                    pdkey_idx = pkey_idx + offset
                    if 0 <= pdkey_idx < len(cls._ln_array):
                        pdkey = cls._ln_array[pdkey_idx]
                        if pdkey and pdkey.isalnum() and len(pdkey) >= 8:
                            return cls._mouflon_keys.setdefault(pkey, pdkey)
        
        return None

    @staticmethod
    def _getMouflonFromM3U(m3u8_doc):
        _start = 0
        _needle = '#EXT-X-MOUFLON:'
        while _needle in (_doc := m3u8_doc[_start:]):
            _mouflon_start = _doc.find(_needle)
            if _mouflon_start > 0:
                _mouflon = _doc[_mouflon_start:m3u8_doc.find('\n', _mouflon_start)].strip().split(':')
                psch = _mouflon[2]
                pkey = _mouflon[3]
                pdkey = StripChat.getMouflonDecKey(pkey)
                if pdkey:
                    return psch, pkey, pdkey
            _start += _mouflon_start + len(_needle)
        return None, None, None

    @staticmethod
    def uniq():
        """Generate a random unique string for API requests."""
        chars = ''.join(chr(i) for i in range(ord('a'), ord('z') + 1))
        chars += ''.join(chr(i) for i in range(ord('0'), ord('9') + 1))
        return ''.join(random.choice(chars) for _ in range(16))

    @staticmethod
    def normalizeInfo(raw: dict) -> dict:
        """
        Normalize JSON so lastInfo is always a dict.
        Keep top-level where possible; flatten only obvious wrappers like `item` or single-element lists.
        """
        if not raw:
            return {}
        # If it's a list, use first element (common wrapping)
        if isinstance(raw, list):
            return raw[0] if raw else {}
        # If it's a dict with 'item' containing the real object
        if isinstance(raw, dict) and "item" in raw and isinstance(raw["item"], dict):
            return raw["item"]
        # If it's a dict with a single top wrapper like {"data": {...}} try common keys but avoid dropping cam/user
        if isinstance(raw, dict) and len(raw) == 1:
            sole_key = next(iter(raw))
            if sole_key in ("data", "result", "response") and isinstance(raw[sole_key], dict):
                return raw[sole_key]
        # Otherwise return as-is (we'll handle nested shapes in getters)
        return raw

    def _get_by_path(self, data: dict, path: list):
        """
        Safely get nested value by path list from dict-like structures.
        Returns None if any step is missing or type mismatch.
        """
        cur = data
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None
        return cur

    def _recursive_find(self, data, key):
        """
        Depth-first search for first occurrence of `key` in nested dict/list objects.
        Returns the value if found, otherwise None.
        """
        if isinstance(data, dict):
            if key in data:
                return data[key]
            for v in data.values():
                found = self._recursive_find(v, key)
                if found is not None:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = self._recursive_find(item, key)
                if found is not None:
                    return found
        return None

    def _first_in_paths(self, paths: list):
        """
        Try a list of paths (each path is a list of keys). Return first non-None value.
        """
        for p in paths:
            val = self._get_by_path(self.lastInfo, p)
            if val is not None:
                return val
        return None
    

    def getWebsiteURL(self):
        return "https://stripchat.com/" + self.username

    def getVideoUrl(self):
        return self.getWantedResolutionPlaylist(None)

    def getStreamName(self) -> str:
        """
        Robust streamName detector: checks multiple common locations and finally
        performs a recursive search for the key if not found.
        Raises KeyError if nothing found.
        """
        if not self.lastInfo:
            raise KeyError("lastInfo is empty, call getStatus() first")

        # Common paths to check in order
        paths = [
            ["streamName"],
            ["cam", "streamName"],
            ["user", "streamName"],
            ["user", "user", "streamName"],
            ["model", "streamName"],
            ["user", "user", "userStreamName"],
            ["cam", "userStreamName"],
        ]
        val = self._first_in_paths(paths)
        if val:
            return str(val)

        # Last resort: recursive find
        val = self._recursive_find(self.lastInfo, "streamName")
        if val:
            return str(val)

        raise KeyError(f"No streamName in lastInfo: keys={list(self.lastInfo.keys())}")

    def getStatusField(self):
        """
        Robust status detector. Tries known paths and falls back to recursive search.
        Returns the raw status value or None.
        """
        if not self.lastInfo:
            return None

        # Explicit candidate paths (ordered)
        paths = [
            ["status"],
            ["cam", "streamStatus"],
            ["cam", "status"],
            ["model", "status"],
            ["user", "status"],
            ["user", "user", "status"],
            ["user", "user", "state"],
            ["user", "user", "broadcastStatus"],
        ]
        status = self._first_in_paths(paths)
        if status is not None:
            return status

        # Recursive fallback — but prefer strings that match expected status tokens
        found = self._recursive_find(self.lastInfo, "status")
        if isinstance(found, str):
            return found
        return None

    def getIsLive(self) -> bool:
        """
        Robust isLive detector. Checks multiple locations and returns boolean.
        """
        if not self.lastInfo:
            return False

        # Try direct root flag
        val = self._get_by_path(self.lastInfo, ["isLive"])
        if val is not None:
            return bool(val)

        # Common locations
        paths = [
            ["cam", "isCamActive"],
            ["cam", "isCamAvailable"],
            ["cam", "isLive"],
            ["model", "isLive"],
            ["user", "isLive"],
            ["user", "user", "isLive"],
            ["user", "user", "isCamActive"],
            ["broadcastSettings", "isLive"],
            ["cam", "broadcastSettings", "isCamActive"],
            ["cam", "broadcastSettings", "isLive"],
        ]
        val = self._first_in_paths(paths)
        if val is not None:
            return bool(val)

        # Recursive fallback for any key named isLive or isCamActive
        for k in ("isLive", "isCamActive", "isCamAvailable"):
            found = self._recursive_find(self.lastInfo, k)
            if found is not None:
                return bool(found)

        return False

    def getIsMobile(self) -> bool:
        """
        Robust isMobile detector. Checks multiple locations and returns boolean.
        """
        if not self.lastInfo:
            return False

        # Direct
        val = self._get_by_path(self.lastInfo, ["isMobile"])
        if val is not None:
            return bool(val)

        # Common paths
        paths = [
            ["model", "isMobile"],
            ["user", "isMobile"],
            ["user", "user", "isMobile"],
            ["broadcastSettings", "isMobile"],
            ["cam", "broadcastSettings", "isMobile"],
            ["cam", "isMobile"],
        ]
        val = self._first_in_paths(paths)
        if val is not None:
            return bool(val)

        # Recursive fallback
        found = self._recursive_find(self.lastInfo, "isMobile")
        if found is not None:
            return bool(found)

        return False

    def getIsGeoBanned(self) -> bool:
        """Check if user is geo-banned from viewing this model."""
        if not self.lastInfo:
            return False

        paths = [
            ["isGeoBanned"],
            ["user", "isGeoBanned"],
            ["user", "user", "isGeoBanned"],
        ]
        val = self._first_in_paths(paths)
        if val is not None:
            return bool(val)

        found = self._recursive_find(self.lastInfo, "isGeoBanned")
        return bool(found) if found is not None else False

    def getIsDeleted(self) -> bool:
        """Check if the model account has been deleted."""
        if not self.lastInfo:
            return False

        # Try common paths where isDeleted might be found
        paths = [
            ["isDeleted"],
            ["user", "isDeleted"],
            ["user", "user", "isDeleted"],
            ["model", "isDeleted"],
        ]
        val = self._first_in_paths(paths)
        if val is not None:
            return bool(val)

        # Recursive fallback
        found = self._recursive_find(self.lastInfo, "isDeleted")
        return bool(found) if found is not None else False

    def getStatus(self):
        """Check the current status of the model's stream."""
        url = f'https://stripchat.com/api/front/v2/models/username/{self.username}/cam?uniq={StripChat.uniq()}'
        r = self.session.get(url, headers=self.headers, bucket='api')

        ct = (r.headers.get("content-type") or "").lower()
        body = r.text or ""

        # Handle HTTP errors
        if r.status_code == 404:
            return Status.NOTEXIST
        if r.status_code == 403:
            if looks_like_cf_html(body):
                self.logger.error(f'Cloudflare challenge (403) for {self.username}')
                return Status.CLOUDFLARE
            return Status.RESTRICTED
        if r.status_code == 429:
            self.logger.error(f'Rate limited (429) for {self.username}')
            return Status.RATELIMIT
        if r.status_code >= 500:
            if looks_like_cf_html(body):
                self.logger.error(f'Cloudflare challenge ({r.status_code}) for {self.username}')
                return Status.CLOUDFLARE
            self.logger.error(f'Server error {r.status_code} for {self.username}')
            return Status.UNKNOWN

        # Validate JSON response
        if "application/json" not in ct or not body.strip():
            self.logger.warning(f'Non-JSON response for {self.username}')
            return Status.UNKNOWN

        try:
            raw = r.json()
        except Exception as e:
            self.logger.error(f'Failed to parse JSON for {self.username}: {e}')
            return Status.UNKNOWN

        # Normalize and store info
        self.lastInfo = self.normalizeInfo(raw)

        # Check for geo-ban
        if self.getIsGeoBanned():
            return Status.RESTRICTED

        # Check if model account has been deleted
        if self.getIsDeleted():
            self.logger.warning(f'⚠️ Model account {self.username} has been deleted - this model will be auto-deregistered')
            return Status.DELETED

        # Determine status - check isLive first as it's more reliable
        is_live = self.getIsLive()
        is_cam_available = self._get_by_path(self.lastInfo, ["isCamAvailable"]) or \
                          self._get_by_path(self.lastInfo, ["cam", "isCamAvailable"]) or False
        
        # If not live, check if camera is available (model is online but no stream yet)
        if not is_live:
            if is_cam_available:
                # Model is connected and ready but hasn't started streaming
                return Status.ONLINE
            # Not live and no camera available = offline
            return Status.OFFLINE
        
        # If live, check the status field
        status = self.getStatusField()
        if status == "public":
            return Status.PUBLIC
        if status in ["private", "groupShow", "p2p", "virtualPrivate", "p2pVoice"]:
            return Status.PRIVATE
        
        # Edge case: is_live=true but status is unclear - default to private to be safe
        if is_live and status is None:
            return Status.PRIVATE
        
        # Status is set to something unexpected
        if status in ["off", "idle"]:
            return Status.OFFLINE
        
        # Unknown status - log the actual data for debugging
        self.logger.warning(f"Unknown status '{status}' for {self.username} - lastInfo keys: {list(self.lastInfo.keys())}")
        self.logger.debug(f"Full response for {self.username}: {str(self.lastInfo)[:500]}")
        return Status.UNKNOWN

    def isMobile(self):
        """Check if the current broadcast is from a mobile device."""
        return self.getIsMobile()

    def getPlaylistVariants(self, url):
        url = "https://edge-hls.{host}/hls/{id}{vr}/master/{id}{vr}{auto}.m3u8".format(
                host='doppiocdn.' + random.choice(['org', 'com', 'net']),
                id=self.getStreamName(),
                vr='_vr' if self.vr else '',
                auto='_auto' if not self.vr else ''
            )
        result = requests.get(url, headers=self.headers, cookies=self.cookies)
        m3u8_doc = result.content.decode("utf-8")
        psch, pkey, pdkey = StripChat._getMouflonFromM3U(m3u8_doc)
        self.debug(f"Extracted key {psch}, {pkey}, {pdkey}")
        variants = super().getPlaylistVariants(m3u_data=m3u8_doc)
        return [
            variant
            | {
                "url": f"{variant['url']}{'&' if '?' in variant['url'] else '?'}psch={psch}&pkey={pkey}"
            }
            for variant in variants
        ]

Bot.loaded_sites.add(StripChat)