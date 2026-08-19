#!/usr/bin/env bash
#
# aero-opt-agent — chaine CFD complete pour une iteration.
#
#   openfoam/run_cfd.sh --iteration-dir data/iterations/iter_0000
#
# Enchaine : construction du case, blockMesh, surfaceFeatureExtract,
# snappyHexMesh, checkMesh (OBLIGATOIRE), simpleFoam, puis post-traitement
# vers results.json.
#
# Le script ne calcule RIEN lui-meme : le dimensionnement du case est fait par
# case_builder.py et l extraction des coefficients par postprocess.py. Il
# orchestre, journalise, et s arrete au premier echec avec un statut clair.
#
# Codes de retour :
#   0  succes
#   1  echec (la cause precise est dans results.json et dans les logs)
#   2  usage / environnement (OpenFOAM absent, arguments invalides)
#
set -u -o pipefail

# ─────────────────────────────────────────────────────────────
# Arguments
# ─────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ITERATION_DIR=""
DESIGN_PARAMS="$REPO_ROOT/configs/design_params.yaml"
CFD_SETTINGS="$REPO_ROOT/configs/cfd_settings.yaml"
SKIP_SOLVER=0
DRY_RUN=0

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --iteration-dir) ITERATION_DIR="${2:-}"; shift 2 ;;
        --design-params) DESIGN_PARAMS="${2:-}"; shift 2 ;;
        --cfd-settings)  CFD_SETTINGS="${2:-}";  shift 2 ;;
        --mesh-only)     SKIP_SOLVER=1; shift ;;
        --dry-run)       DRY_RUN=1; shift ;;
        -h|--help)       usage ;;
        *) echo "Argument inconnu : $1" >&2; usage ;;
    esac
done

[ -n "$ITERATION_DIR" ] || { echo "--iteration-dir est obligatoire" >&2; usage; }
[ -d "$ITERATION_DIR" ] || { echo "Dossier d iteration absent : $ITERATION_DIR" >&2; exit 2; }

ITERATION_DIR="$(cd "$ITERATION_DIR" && pwd)"
CASE_DIR="$ITERATION_DIR/cfd"
LOG_DIR="$ITERATION_DIR/logs"
RESULTS="$ITERATION_DIR/results.json"
PYTHON="${PYTHON:-python3}"

mkdir -p "$LOG_DIR"

# ─────────────────────────────────────────────────────────────
# Journalisation
# ─────────────────────────────────────────────────────────────
RUN_LOG="$LOG_DIR/run_cfd.log"

log()  { printf '%s [INFO] %s\n'  "$(date +%H:%M:%S)" "$*" | tee -a "$RUN_LOG"; }
warn() { printf '%s [WARN] %s\n'  "$(date +%H:%M:%S)" "$*" | tee -a "$RUN_LOG" >&2; }
err()  { printf '%s [ERROR] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$RUN_LOG" >&2; }

# Ecrit un results.json d echec puis sort. Le pipeline et l agent ne lisent que
# ce fichier : une sortie en erreur sans results.json les laisserait aveugles.
fail() {
    local status="$1"; shift
    local message="$*"
    err "[$status] $message"
    "$PYTHON" "$REPO_ROOT/openfoam/postprocess.py" \
        --iteration-dir "$ITERATION_DIR" \
        --design-params "$DESIGN_PARAMS" \
        --failure-status "$status" \
        --failure-message "$message" >/dev/null 2>&1 || {
            # Dernier recours : results.json minimal ecrit a la main.
            printf '{\n  "success": false,\n  "status": "%s",\n  "error_message": "%s",\n  "mesh_ok": false\n}\n' \
                "$status" "${message//\"/\\\"}" > "$RESULTS"
        }
    exit 1
}

# Lance une commande OpenFOAM, journalise, verifie le code retour ET la
# presence de "FOAM FATAL" — certains utilitaires sortent en 0 apres une
# erreur fatale.
run_foam() {
    local name="$1"; shift
    local log_file="$LOG_DIR/$name.log"
    log "-> $name"
    if [ "$DRY_RUN" -eq 1 ]; then
        log "   (dry-run : $* non execute)"
        return 0
    fi
    ( cd "$CASE_DIR" && "$@" ) > "$log_file" 2>&1
    local rc=$?
    if [ $rc -ne 0 ]; then
        local tail_msg
        tail_msg="$(tail -n 15 "$log_file" | tr '\n' ' ' | tr -s ' ')"
        fail "${name^^}_FAILED" "$name a echoue (code $rc) : ${tail_msg}"
    fi
    if grep -q "FOAM FATAL" "$log_file"; then
        local fatal
        fatal="$(grep -A 4 'FOAM FATAL' "$log_file" | head -n 8 | tr '\n' ' ' | tr -s ' ')"
        fail "${name^^}_FAILED" "$name : erreur fatale OpenFOAM : ${fatal}"
    fi
    log "   $name OK ($(wc -l < "$log_file") lignes de journal)"
}

# ─────────────────────────────────────────────────────────────
# Environnement OpenFOAM
# ─────────────────────────────────────────────────────────────
log "=== CFD — $ITERATION_DIR ==="

if ! command -v blockMesh >/dev/null 2>&1; then
    if [ -n "${FOAM_BASHRC:-}" ] && [ -f "${FOAM_BASHRC}" ]; then
        log "Chargement de l environnement OpenFOAM : $FOAM_BASHRC"
        # shellcheck disable=SC1090
        set +u; . "$FOAM_BASHRC"; set -u
    else
        for candidate in /usr/lib/openfoam/openfoam*/etc/bashrc /opt/openfoam*/etc/bashrc; do
            if [ -f "$candidate" ]; then
                log "Chargement de l environnement OpenFOAM : $candidate"
                # shellcheck disable=SC1090
                set +u; . "$candidate"; set -u
                break
            fi
        done
    fi
fi

if [ "$DRY_RUN" -eq 0 ] && ! command -v blockMesh >/dev/null 2>&1; then
    fail "OPENFOAM_MISSING" \
        "OpenFOAM introuvable. Definir FOAM_BASHRC dans .env (ex. /usr/lib/openfoam/openfoam2312/etc/bashrc) ou installer OpenFOAM."
fi

SOLVER="$("$PYTHON" - "$CFD_SETTINGS" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print((cfg.get("case") or {}).get("solver", "simpleFoam"))
PY
)" || fail "CONFIG_ERROR" "lecture impossible de $CFD_SETTINGS"

N_PROCS="$("$PYTHON" - "$CFD_SETTINGS" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print(int((cfg.get("execution") or {}).get("n_processors", 1)))
PY
)" || fail "CONFIG_ERROR" "lecture impossible de $CFD_SETTINGS"

if [ "$DRY_RUN" -eq 0 ] && ! command -v "$SOLVER" >/dev/null 2>&1; then
    fail "SOLVER_MISSING" \
        "solveur '$SOLVER' absent de cette installation OpenFOAM. Les versions OpenFOAM.org >= 11 ont remplace simpleFoam par 'foamRun -solver incompressibleFluid' : ajuster case.solver dans cfd_settings.yaml."
fi

# ─────────────────────────────────────────────────────────────
# 1. Construction du case
# ─────────────────────────────────────────────────────────────
log "-> case_builder"
if ! "$PYTHON" "$REPO_ROOT/openfoam/case_builder.py" \
        --iteration-dir "$ITERATION_DIR" \
        --design-params "$DESIGN_PARAMS" \
        --cfd-settings "$CFD_SETTINGS" \
        > "$LOG_DIR/case_builder.log" 2>&1; then
    STATUS="$("$PYTHON" -c "
import json,sys
try:
    print(json.load(open('$LOG_DIR/case_builder.log')).get('status','CASE_BUILD_FAILED'))
except Exception:
    print('CASE_BUILD_FAILED')
" 2>/dev/null || echo CASE_BUILD_FAILED)"
    fail "$STATUS" "preparation du case impossible : $(tail -n 20 "$LOG_DIR/case_builder.log" | tr '\n' ' ' | tr -s ' ')"
fi
log "   case pret : $CASE_DIR"

# ─────────────────────────────────────────────────────────────
# 2. Maillage
# ─────────────────────────────────────────────────────────────
run_foam blockMesh blockMesh
run_foam surfaceFeatureExtract surfaceFeatureExtract

if [ "$N_PROCS" -gt 1 ] && command -v mpirun >/dev/null 2>&1; then
    log "Maillage parallele sur $N_PROCS coeurs"
    run_foam decomposePar decomposePar -force
    run_foam snappyHexMesh mpirun -np "$N_PROCS" snappyHexMesh -overwrite -parallel
    run_foam reconstructParMesh reconstructParMesh -constant -mergeTol 1e-6
    ( cd "$CASE_DIR" && rm -rf processor* )
else
    [ "$N_PROCS" -gt 1 ] && warn "mpirun absent : maillage en sequentiel"
    run_foam snappyHexMesh snappyHexMesh -overwrite
fi

# ─────────────────────────────────────────────────────────────
# 3. checkMesh — OBLIGATOIRE (Master Doc §4.3)
# ─────────────────────────────────────────────────────────────
log "-> checkMesh"
if [ "$DRY_RUN" -eq 0 ]; then
    ( cd "$CASE_DIR" && checkMesh -allGeometry -allTopology ) \
        > "$LOG_DIR/checkMesh.log" 2>&1 || true
    if grep -qE "^\s*Failed [0-9]+ mesh checks" "$LOG_DIR/checkMesh.log"; then
        FAILED_LINE="$(grep -E "^\s*Failed [0-9]+ mesh checks" "$LOG_DIR/checkMesh.log" | head -1 | tr -s ' ')"
        FAIL_ON_ERROR="$("$PYTHON" - "$CFD_SETTINGS" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print(int(bool(((cfg.get("mesh") or {}).get("check_mesh") or {}).get("fail_on_error", True))))
PY
)"
        if [ "$FAIL_ON_ERROR" = "1" ]; then
            fail "MESH_CHECK_FAILED" "checkMesh :${FAILED_LINE}. Le maillage est invalide, inutile de lancer le solveur — proposer une variation plus conservative."
        else
            warn "checkMesh :${FAILED_LINE} (fail_on_error=false, on continue)"
        fi
    else
        log "   maillage valide"
    fi
fi

if [ "$SKIP_SOLVER" -eq 1 ]; then
    log "--mesh-only : arret avant le solveur"
    exit 0
fi

# ─────────────────────────────────────────────────────────────
# 4. Solveur
# ─────────────────────────────────────────────────────────────
MAX_WALL="$("$PYTHON" - "$CFD_SETTINGS" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print(int((cfg.get("solver_control") or {}).get("max_wall_time_s", 5400)))
PY
)"

TIMEOUT_CMD=""
if command -v timeout >/dev/null 2>&1 && [ "$MAX_WALL" -gt 0 ]; then
    TIMEOUT_CMD="timeout --signal=TERM ${MAX_WALL}s"
    log "Garde-fou temps de calcul : ${MAX_WALL}s"
fi

if [ "$N_PROCS" -gt 1 ] && command -v mpirun >/dev/null 2>&1; then
    run_foam decomposeParRun decomposePar -force
    log "-> $SOLVER (parallele, $N_PROCS coeurs)"
    if [ "$DRY_RUN" -eq 0 ]; then
        ( cd "$CASE_DIR" && $TIMEOUT_CMD mpirun -np "$N_PROCS" "$SOLVER" -parallel ) \
            > "$LOG_DIR/$SOLVER.log" 2>&1
        rc=$?
        [ $rc -eq 124 ] && fail "SOLVER_TIMEOUT" "$SOLVER interrompu apres ${MAX_WALL}s sans converger"
        [ $rc -ne 0 ] && fail "SOLVER_FAILED" "$SOLVER a echoue (code $rc) : $(tail -n 15 "$LOG_DIR/$SOLVER.log" | tr '\n' ' ' | tr -s ' ')"
        run_foam reconstructPar reconstructPar -latestTime
        ( cd "$CASE_DIR" && rm -rf processor* )
    fi
else
    log "-> $SOLVER (sequentiel)"
    if [ "$DRY_RUN" -eq 0 ]; then
        ( cd "$CASE_DIR" && $TIMEOUT_CMD "$SOLVER" ) > "$LOG_DIR/$SOLVER.log" 2>&1
        rc=$?
        [ $rc -eq 124 ] && fail "SOLVER_TIMEOUT" "$SOLVER interrompu apres ${MAX_WALL}s sans converger"
        [ $rc -ne 0 ] && fail "SOLVER_FAILED" "$SOLVER a echoue (code $rc) : $(tail -n 15 "$LOG_DIR/$SOLVER.log" | tr '\n' ' ' | tr -s ' ')"
    fi
fi

# ─────────────────────────────────────────────────────────────
# 5. Post-traitement -> results.json
# ─────────────────────────────────────────────────────────────
log "-> postprocess"
if ! "$PYTHON" "$REPO_ROOT/openfoam/postprocess.py" \
        --iteration-dir "$ITERATION_DIR" \
        --design-params "$DESIGN_PARAMS" \
        --cfd-settings "$CFD_SETTINGS" \
        > "$LOG_DIR/postprocess.log" 2>&1; then
    fail "POSTPROCESS_FAILED" "extraction des coefficients impossible : $(tail -n 20 "$LOG_DIR/postprocess.log" | tr '\n' ' ' | tr -s ' ')"
fi

log "=== Termine — $RESULTS ==="
"$PYTHON" -c "
import json
r = json.load(open('$RESULTS'))
print('Cd = %.5f   Cl = %.5f   Cl/Cd = %.2f   converge = %s'
      % (r['Cd'], r['Cl'], r['Cl_Cd'], r.get('converged')))
" | tee -a "$RUN_LOG"

exit 0
