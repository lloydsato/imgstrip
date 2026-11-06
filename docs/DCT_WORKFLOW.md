# JPEG coefficient workflow

imgstrip's JPEG coefficient options work on the quantised DCT coefficients inside a JPEG, where steganography tools such as JSteg, OutGuess, steghide, F5 and J-UNIWARD hide data. `--jpeg-canonical` rebuilds the file in one canonical form and leaves the pixels unchanged, and `--scrub-dct` adds a scrub of the coefficient values that costs picture quality. Both are opt-in and off by default. The diagrams below follow one run from the entry point to the reported result.

### A. End-to-end workflow

```mermaid
flowchart TD
    subgraph ENTRY ["Entry"]
        U1["CLI with --scrub-dct or --jpeg-canonical"]
        U2["Web switches or JSON flags scrub_dct and jpeg_canonical"]
        OPT["Options with scrub_dct and dct_min_psnr"]
    end

    subgraph DISPATCH ["Dispatch"]
        MODE{"core dct_mode normalises the value"}
        BAD["StripError unknown DCT mode"]
        OFF["Mode off, existing re-encode or bit-exact path"]
        CHK{"Input and output are JPEG and no scrub-prnu"}
        NOJ["StripError not JPEG or combined with scrub-prnu"]
        LL["_jpeg_lossless handles ICC and orientation"]
    end

    subgraph CODEC ["Codec"]
        RD["dct.read decodes the first image to quantised coefficients"]
        REF["StripError and nothing written for arithmetic, lossless, 12-bit, DNL, oversize or corrupt input"]
        MODE2{"Mode from dispatch"}
        CAN["canonical branch, coefficients kept"]
        SCR["full branch, dct.scrub with rng seeded from os.urandom"]
        FLOOR{"Estimated PSNR at least dct_min_psnr"}
        LOW["StripError estimated loss above the limit"]
        WR["dct.write canonical baseline scan"]
        RB["jpeg.rebuild keeps JFIF and Adobe, writes orientation EXIF, sanitised ICC, canonical body and EOI"]
    end

    subgraph VERIFY ["Verification"]
        INS{"inspect_bytes finds no leftover metadata"}
        VINS["VerificationError nothing written"]
        PIL{"Pillow decodes original and result to same size and mode"}
        VPIL["VerificationError geometry changed"]
        VC{"Mode"}
        VCAN["canonical branch, decoded pixels identical and visible coefficients equal"]
        VFULL["full branch, written coefficients match scrubbed frame and check_scrub passes"]
        VPSNR{"Measured PSNR not wildly below the estimate and at least dct_min_psnr"}
        VBAD["VerificationError or StripError nothing written"]
    end

    subgraph REPORT ["Reporting"]
        RES["Result with dct_scrub, dct_psnr, pixels_exact, verified_exact"]
        SR["stego.dct_report headline, summary, caveat"]
        CLI["CLI prints a jpeg coefficients line and a dct key in JSON"]
        WEB["Web result dct field and tooltip"]
        CC["Content Credentials optional, operation dct-scrubbed, integrity altered or identical"]
    end

    U1 --> OPT
    U2 --> OPT
    OPT --> MODE
    MODE -->|"invalid"| BAD
    MODE -->|"off"| OFF
    MODE -->|"canonical or full"| CHK
    CHK -->|"no"| NOJ
    CHK -->|"yes"| LL
    LL --> RD
    RD -->|"JpegError"| REF
    RD --> MODE2
    MODE2 -->|"canonical"| CAN
    MODE2 -->|"full"| SCR
    SCR --> FLOOR
    FLOOR -->|"no"| LOW
    FLOOR -->|"yes"| WR
    CAN --> WR
    WR --> RB
    RB --> INS
    INS -->|"no"| VINS
    INS -->|"yes"| PIL
    PIL -->|"no"| VPIL
    PIL -->|"yes"| VC
    VC -->|"canonical"| VCAN
    VC -->|"full"| VFULL
    VFULL --> VPSNR
    VPSNR -->|"no"| VBAD
    VPSNR -->|"yes"| RES
    VCAN --> RES
    OFF --> RES
    RES --> SR
    SR --> CLI
    SR --> WEB
    SR --> CC

    classDef refuse fill:#fde8e8,stroke:#b42318,color:#7a1212
    classDef scrub fill:#fff4e5,stroke:#b54708,color:#7a2e0e
    classDef ok fill:#e6f4ea,stroke:#1e7b34,color:#114d22
    class BAD,NOJ,REF,LOW,VINS,VPIL,VBAD refuse
    class SCR scrub
    class RES ok
```

An option set from the CLI or the web API reaches `core.dct_mode`, which returns off, canonical or full. The JPEG path refuses inputs or outputs that are not JPEG and any combination with `--scrub-prnu`; when both CLI flags are given, `--scrub-dct` wins over `--jpeg-canonical`. Every red exit happens before a byte is written, and a result is returned only after the verification checks pass.

### B. Codec internals

```mermaid
flowchart TD
    subgraph READ ["dct.read (decode)"]
        RIN["JPEG bytes that start with SOI"]
        RWALK["Marker walk, skipping RST and fill bytes, reading length-prefixed segments"]
        RDQT["DQT quantisation tables"]
        RDHT["DHT Huffman tables with 16-bit look-ahead lookup"]
        RDRI["DRI restart interval"]
        RSOF["SOF0, SOF1 or SOF2 frame header with 8-bit samples, sampling factors and pixel limit"]
        RSOS["SOS scan header with component slots and Ss Se Ah Al"]
        RSPLIT["Split entropy data at RST markers and unstuff 0xFF 0x00"]
        RKIND{"Scan kind"}
        RSEQ["Sequential or extended, all coefficients in one pass"]
        RDCF["Progressive DC first"]
        RDCR["Progressive DC refine"]
        RACF["Progressive AC first with EOB runs"]
        RACR["Progressive AC refine with correction bits"]
        RFRAME["Frame of int16 coefficients per component"]
        RREF["Refused for arithmetic, lossless, hierarchical, DNL, 12-bit, corrupt or oversize data"]
    end

    subgraph WRITE ["dct.write (canonical form)"]
        WIN["Frame coefficients and quantisation tables"]
        WPAD["Padding canonicalised, blocks outside the picture get AC zero and DC of the nearest visible block"]
        WZZ["Coefficients in MCU order and zigzag order"]
        WTOK["Tokenise with DC difference and size, AC run and size, ZRL only before a non-zero, EOB only when a block ends early"]
        WSET["Histograms per table set, set 0 for component 0 and set 1 for the others"]
        WHUF["Optimal Huffman tables (Annex K.2, 16-bit limit) per set"]
        WPACK["Bit packing, most significant bit first, last byte padded with ones"]
        WSTUFF["Byte stuffing, 0xFF becomes 0xFF 0x00"]
        WSEG["Segments with DQT, duplicate tables merged, SOF0 or SOF1, component ids 1 to n (or R, G, B kept), DHT and SOS"]
        WOUT["One interleaved baseline scan with no restart markers and no EOI"]
    end

    RIN --> RWALK
    RWALK --> RDQT
    RWALK --> RDHT
    RWALK --> RDRI
    RWALK --> RSOF
    RWALK --> RSOS
    RWALK -->|"DNL or bad data"| RREF
    RSOF -->|"arithmetic, lossless, hierarchical or 12-bit"| RREF
    RDQT -.-> RSOS
    RDHT -.-> RSOS
    RDRI -.-> RSPLIT
    RSOF -.-> RSOS
    RSOS --> RSPLIT
    RSPLIT --> RKIND
    RKIND -->|"baseline or extended"| RSEQ
    RKIND -->|"progressive Ss 0 Ah 0"| RDCF
    RKIND -->|"progressive Ss 0 Ah above 0"| RDCR
    RKIND -->|"progressive Ss above 0 Ah 0"| RACF
    RKIND -->|"progressive Ss above 0 Ah above 0"| RACR
    RSEQ --> RFRAME
    RDCF --> RFRAME
    RDCR --> RFRAME
    RACF --> RFRAME
    RACR --> RFRAME
    RFRAME -.->|"coefficients handed to"| WIN
    WIN --> WPAD
    WPAD --> WZZ
    WZZ --> WTOK
    WTOK --> WSET
    WSET --> WHUF
    WHUF --> WPACK
    WPACK --> WSTUFF
    WSTUFF --> WSEG
    WSEG --> WOUT

    classDef refuse fill:#fde8e8,stroke:#b42318,color:#7a1212
    class RREF refuse
```

`dct.read` is the codec's own decoder: it walks the markers, builds the Huffman lookup tables, and decodes each scan with the routine that matches its kind, splitting entropy data at restart markers first. `dct.write` runs the other way and throws away every encoder choice: it canonicalises the padding blocks, tokenises each block, builds optimal Huffman tables for two table sets, and packs the bits into one interleaved baseline scan. Because the output depends only on the coefficients, the quantisation tables and the geometry, the original encoder's table layout, padding and markers do not survive.

### C. What each layer closes

```mermaid
flowchart LR
    subgraph HIDE ["Hiding places"]
        H1["Huffman table layout"]
        H2["Unused Huffman tables"]
        H3["EOB and ZRL aliases"]
        H4["Padding bits"]
        H5["Restart markers"]
        H6["Progressive scan script"]
        H7["Blocks outside the picture"]
        H8["Component ids and quantisation table numbering"]
        H9["Coefficient parity"]
        H10["Coefficient support, meaning which values are zero or plus or minus one"]
    end

    subgraph LAYER ["Layer that closes it"]
        LA["Layer A canonical write, pixels unchanged"]
        LB["Layer B scrub, coefficient values change"]
    end

    subgraph OPEN ["Stays uncovered"]
        UN["Robust watermarks, payloads in picture content, ternary mod-3 embedders, second-order leakage from plus-or-minus-one embedders"]
    end

    H1 --> LA
    H2 --> LA
    H3 --> LA
    H4 --> LA
    H5 --> LA
    H6 --> LA
    H7 --> LA
    H8 --> LA
    H9 --> LB
    H10 --> LB
    LA -->|"included in --scrub-dct"| LB

    classDef layerA fill:#e6f4ea,stroke:#1e7b34,color:#114d22
    classDef layerB fill:#fff4e5,stroke:#b54708,color:#7a2e0e
    classDef uncovered fill:#fde8e8,stroke:#b42318,color:#7a1212
    class LA layerA
    class LB layerB
    class UN uncovered
```

Layer A, the canonical write, closes every hiding place around the coefficients and leaves the decoded pixels unchanged, so `--jpeg-canonical` costs nothing in picture quality but keeps the coefficients. Layer B, which only `--scrub-dct` runs (after Layer A), closes the two channels that live in the coefficient values themselves: their parity, and the support pattern used by zero-changing embedders such as J-UNIWARD. The red node is the honest limit: robust watermarks, payloads carried by the picture content, ternary embedders and the leak of plus-or-minus-one embedders at the edge of the zero zone (finding F5: about 0.08 bit per carrier for a receiver who knows the carrier positions, see the reference) are outside what the guarantee covers.

### D. Verification sequence

```mermaid
sequenceDiagram
    participant U as User
    participant K as core
    participant D as dct
    participant P as Pillow
    participant J as jpeg.rebuild
    U->>K: strip with scrub_dct full
    K->>K: dct_mode full and input and output are JPEG
    K->>D: read data with pixel limit
    alt not representable or corrupt
        D-->>K: JpegError
        K-->>U: StripError nothing written
    else decoded
        D-->>K: Frame
        K->>D: scrub frame with rng seeded from os.urandom
        D-->>K: ScrubStats with estimated PSNR
        alt estimate below dct_min_psnr
            K-->>U: StripError nothing written
        else estimate acceptable
            K->>D: write frame
            D-->>K: canonical body
            K->>J: rebuild with orientation ICC and canonical body
            J-->>K: clean JPEG and count of dropped images
            K->>K: inspect_bytes leftover check
            K->>P: decode original and result
            P-->>K: same size and mode
            K->>D: read result
            D-->>K: written Frame
            K->>K: written coefficients equal scrubbed frame and check_scrub passes
            K->>P: decoded arrays for real PSNR
            P-->>K: pixel arrays
            alt real PSNR more than 10 dB below estimate
                K-->>U: VerificationError nothing written
            else real PSNR below dct_min_psnr
                K-->>U: StripError nothing written
            else all checks pass
                K-->>U: Result with dct_scrub scrubbed and dct_psnr
            end
        end
    end
```

A full-mode run reads the input, scrubs the coefficients, writes the canonical form, and then re-reads the written file and checks it against what the scrub promised before anything is returned. The floor is checked twice: on the estimate before writing, and on the PSNR measured from decoded pixels after writing, and the measured value must not fall more than 10 dB below the estimate (the estimate averages error over whole blocks, so it is loose for tiny pictures; the exact coefficient comparison is the strict check). Canonical mode follows the same path but compares decoded pixels and visible coefficients exactly, and any mismatch is a hard failure with nothing written.

## Reading guide

- `src/imgstrip/core.py`: `dct_mode` (normalisation), `_Job._run` (dispatch and refusals), `_jpeg_lossless` (headers and the structural and pixel checks), `_dct_body` (read, scrub, floor, write) and `_verify_dct_scrub` (scrub checks).
- `src/imgstrip/dct.py`: `read` and its scan decoders, `write` with `canonicalise_padding`, `scrub`, `check_scrub` and `psnr`.
- `src/imgstrip/jpeg.py`: `rebuild`, which writes the headers around the canonical body and drops everything else.
- `src/imgstrip/stego.py`: `dct_report`, the wording shared by the CLI, the web app and JSON.
- `src/imgstrip/cli.py` (`options_from_args`; `--dct-min-psnr` defaults to 25 dB, 0 disables) and `src/imgstrip/web/server.py` (`parse_options`). The web API has no PSNR switch, so web runs always use the default floor.
- `src/imgstrip/credentials.py`: records `dct-scrubbed` and sets the integrity claim from a pixel comparison.
- `tests/test_dct_codec.py`, `tests/test_dct_canonical.py`, `tests/test_dct_scrub.py`, `tests/test_dct_edge_cases.py` and `tests/test_dct_surfaces.py`: the tests for the codec, Layer A, Layer B, the edge cases, and the command line, web API and reports. `tests/jpeg_craft.py` and `tests/dct_embedders.py` are the reference encoder (every covert-channel knob) and the reference steganography tools they use.

Cost and limits: the full scrub costs picture quality. The measured figures quoted for this feature are roughly 29 to 39 dB PSNR against the input, with files typically 40 to 70 percent smaller. Canonical mode costs nothing in pixels. Neither mode proves that nothing is hidden; the guarantee covers payloads read from coefficient parity or support, as described in section C.
