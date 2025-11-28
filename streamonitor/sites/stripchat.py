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
    _mouflon_pkey: str = None   # Cached pkey
    _mouflon_pdkey: str = None  # Cached pdkey
    _cached_keys: dict[str, bytes] = None
    _ln_array: list = None  # Decoded string array from obfuscated JS

    def __init__(self, username):
        if StripChat._static_data is None:
            StripChat._static_data = {}
            try:
                StripChat.getInitialData()
            except Exception as e:
                StripChat._static_data = None
                raise e
        while StripChat._static_data == {}:
            time.sleep(1)
        
        # Ensure mouflon keys are available - re-extract if missing
        if not StripChat._mouflon_pkey or not StripChat._mouflon_pdkey:
            print("[StripChat] Warning: Mouflon keys missing, attempting re-extraction...")
            StripChat._parseMouflonKeys()
            if StripChat._mouflon_keys:
                StripChat._mouflon_pkey = next(iter(StripChat._mouflon_keys.keys()))
                StripChat._mouflon_pdkey = StripChat._mouflon_keys[StripChat._mouflon_pkey]
                print(f"[StripChat] Re-extracted keys: pkey={StripChat._mouflon_pkey}, pdkey={StripChat._mouflon_pdkey}")
        
        super().__init__(username)
        self.vr = False
        self.getVideo = lambda _, url, filename: getVideoNativeHLS(self, url, filename, StripChat.m3u_decoder)

    def get_site_color(self):
        """Return the color scheme for this site"""
        return ("green", [])

    @classmethod
    def getInitialData(cls):
        """Fetch static configuration and parse mouflon keys from Doppio JS."""
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
        
        # Cache pkey and pdkey for direct access
        if cls._mouflon_keys:
            cls._mouflon_pkey = next(iter(cls._mouflon_keys.keys()))
            cls._mouflon_pdkey = cls._mouflon_keys[cls._mouflon_pkey]
        else:
            print("[StripChat] Warning: No mouflon keys found - stream decoding may fail")

    @classmethod
    def _parseMouflonKeys(cls):
        """
        Extract mouflon pkey/pdkey from Doppio JS.
        
        The keys are built from a specific formula in the obfuscated JS:
        - Base36 conversions of fixed numbers
        - Character shifts
        - IIFEs that decode numeric arguments
        
        Current known formula (v2.0.10):
        pkey = "Zeechoej4aleeshi"
        pdkey = "ubahjae7goPoodi6"
        """
        if cls._mouflon_keys is None:
            cls._mouflon_keys = {}
        
        js_data = cls._doppio_js_data
        if not js_data:
            return
        
        try:
            # Try to extract keys from the ns expression
            keys = cls._extractNsKeys(js_data)
            if keys:
                cls._mouflon_keys[keys[0]] = keys[1]
                return
            
            # Fallback to legacy patterns
            cls._parseLegacyMouflonKeys()
            
        except Exception as e:
            print(f"[StripChat] Warning: Failed to parse mouflon keys: {e}")
            cls._parseLegacyMouflonKeys()

    @classmethod
    def _extractNsKeys(cls, js_data):
        """
        Extract pkey/pdkey from the ns expression in Doppio JS.
        
        The ns expression uses this pattern:
        1. 16.toString(36) shifted by -13 = 'Z'
        2. 0x531f77594da7d.toString(36) = 'eechoej4al'
        3. 18676.toString(36) = 'ees'
        4. IIFE(33,164,172,...) = 'hi:ubahja'
        5. 662856.toString(36) = 'e7go'
        6. 32.toString(36) shifted by -39 = 'P'
        7. 31981.toString(36) = 'ood'
        8. IIFE(40,151,201) = 'i6'
        
        Result: "Zeechoej4aleeshi:ubahjae7goPoodi6" split by ':'
        """
        # Check for ns pattern
        if 'const ns=' not in js_data:
            return None
        
        def to_base36(n):
            chars = '0123456789abcdefghijklmnopqrstuvwxyz'
            if n == 0:
                return '0'
            result = ''
            while n > 0:
                result = chars[n % 36] + result
                n //= 36
            return result
        
        # Find the two IIFEs with their arguments
        # First IIFE: }(33,164,172,169,161,161,179,119,165,163)
        iife1_match = re.search(r'\}\((\d+(?:,\d+){8,12})\)', js_data)
        # Second IIFE: }(40,151,201)
        iife2_match = re.search(r'\}\((\d{2},\d{3},\d{3})\)', js_data)
        
        if not iife1_match or not iife2_match:
            return None
        
        # Decode first IIFE: (n, args...) -> reversed, (arg - n - 26) - idx
        args1 = [int(x) for x in iife1_match.group(1).split(',')]
        n1 = args1[0]
        remaining1 = args1[1:][::-1]  # reverse
        p4 = ''.join(chr((a - n1 - 26) - i) for i, a in enumerate(remaining1))
        
        # Decode second IIFE: (o, args...) -> reversed, ((arg - o) - 56) - idx
        args2 = [int(x) for x in iife2_match.group(1).split(',')]
        o2 = args2[0]
        remaining2 = args2[1:][::-1]  # reverse
        p8 = ''.join(chr(((a - o2) - 56) - i) for i, a in enumerate(remaining2))
        
        # Build the key string
        p1 = ''.join(chr(ord(c) - 13) for c in to_base36(16))  # 'Z'
        p2 = to_base36(0x531f77594da7d).lower()  # 'eechoej4al'
        p3 = to_base36(18676).lower()  # 'ees'
        # p4 from IIFE1  # 'hi:ubahja'
        p5 = to_base36(662856).lower()  # 'e7go'
        p6 = ''.join(chr(ord(c) - 39) for c in to_base36(32).lower())  # 'P'
        p7 = to_base36(31981).lower()  # 'ood'
        # p8 from IIFE2  # 'i6'
        
        key_string = p1 + p2 + p3 + p4 + p5 + p6 + p7 + p8
        
        if ':' in key_string:
            pkey, pdkey = key_string.split(':', 1)
            if len(pkey) >= 8 and len(pdkey) >= 8:
                return (pkey, pdkey)
        
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

    @classmethod
    def _getMouflonFromM3U(cls, m3u8_doc):
        """
        Extract psch, pkey, pdkey from m3u8 MOUFLON tags.
        Uses cached class variables for pkey/pdkey.
        """
        # Return cached keys directly - they were extracted once at startup
        if cls._mouflon_pkey and cls._mouflon_pdkey:
            return 'v1', cls._mouflon_pkey, cls._mouflon_pdkey
        
        # Keys missing - try to re-extract
        if cls._doppio_js_data:
            print("[StripChat] Keys missing in _getMouflonFromM3U, attempting re-extraction...")
            cls._parseMouflonKeys()
            if cls._mouflon_keys:
                cls._mouflon_pkey = next(iter(cls._mouflon_keys.keys()))
                cls._mouflon_pdkey = cls._mouflon_keys[cls._mouflon_pkey]
                print(f"[StripChat] Re-extracted: pkey={cls._mouflon_pkey}, pdkey={cls._mouflon_pdkey}")
                return 'v1', cls._mouflon_pkey, cls._mouflon_pdkey
        
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
        playlist_url = "https://edge-hls.{host}/hls/{id}{vr}/master/{id}{vr}{auto}.m3u8".format(
                host='doppiocdn.' + random.choice(['org', 'com', 'net']),
                id=self.getStreamName(),
                vr='_vr' if self.vr else '',
                auto='_auto' if not self.vr else ''
            )
        self.debug(f"Fetching playlist from: {playlist_url}")
        result = requests.get(playlist_url, headers=self.headers, cookies=self.cookies)
        self.debug(f"Playlist response: {result.status_code}")
        
        if result.status_code != 200:
            self.logger.error(f"Failed to fetch playlist: HTTP {result.status_code}")
            self.debug(f"Response body: {result.text[:500]}")
            return []
            
        m3u8_doc = result.content.decode("utf-8")
        self.debug(f"M3U8 content (first 300 chars): {m3u8_doc[:300]}")
        
        psch, pkey, pdkey = StripChat._getMouflonFromM3U(m3u8_doc)
        self.debug(f"Extracted key psch={psch}, pkey={pkey}, pdkey={pdkey}")
        
        if not pkey:
            self.logger.error("No mouflon pkey available - keys not extracted at startup?")
            self.debug(f"Class state: _mouflon_pkey={StripChat._mouflon_pkey}, _mouflon_pdkey={StripChat._mouflon_pdkey}")
            return []
        
        variants = super().getPlaylistVariants(m3u_data=m3u8_doc)
        self.debug(f"Parsed {len(variants) if variants else 0} variants from playlist")
        
        if not variants:
            self.logger.error("No variants found in playlist")
            return []
            
        return [
            variant
            | {
                "url": f"{variant['url']}{'&' if '?' in variant['url'] else '?'}psch={psch}&pkey={pkey}"
            }
            for variant in variants
        ]

Bot.loaded_sites.add(StripChat)