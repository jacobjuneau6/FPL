from pcbnew import *
from functools import reduce

class BgaInfo:
    spacing = 0.0
    rows = 0
    columns = 0
    center = wxPoint(0, 0)
    origin = wxPoint(0, 0)
    pad_grid = None

def detect_spacing(footprint):
    is_first = True
    min_dist = 100000000000
    for pad in footprint.Pads():
        if is_first:
            first_pad = pad
            is_first = False
        elif first_pad.GetPosition().x != pad.GetPosition().x:
            min_dist = min(min_dist, abs(first_pad.GetPosition().x - pad.GetPosition().x))
    return min_dist

def get_first_pad(board, footprint):
    for pad in footprint.Pads():
        netcode = pad.GetNetCode()
        count = 0
        for p in board.GetPads():
            if p.GetNetCode() == netcode:
                count += 1
        if count > 1:
            return pad
    return None

def get_bga_info(footprint):
    info = BgaInfo()
    info.spacing = detect_spacing(footprint)

    pads = footprint.Pads()
    minx = min(p.GetPosition().x for p in pads)
    maxx = max(p.GetPosition().x for p in pads)
    miny = min(p.GetPosition().y for p in pads)
    maxy = max(p.GetPosition().y for p in pads)

    info.origin = wxPoint(minx, miny)
    info.rows = int(1 + round((maxy - miny) / float(info.spacing)))
    info.columns = int(1 + round((maxx - minx) / float(info.spacing)))

    info.pad_grid = {}
    for x in range(info.columns):
        info.pad_grid[x] = {}
        for y in range(info.rows):
            info.pad_grid[x][y] = False
    for pad in pads:
        x = int(round((pad.GetPosition().x - minx) / float(info.spacing)))
        y = int(round((pad.GetPosition().y - miny) / float(info.spacing)))
        info.pad_grid[x][y] = True

    info.center = wxPoint((maxx + minx) // 2, (maxy + miny) // 2)
    return info

def get_pad_position(bga_info, pad):
    offset = pad.GetPosition() - bga_info.center
    return wxPoint(int(offset.x / bga_info.spacing), int(offset.y / bga_info.spacing)) + wxPoint(bga_info.columns // 2, bga_info.rows // 2)

def is_pad_outer_ring(bga_info, pad_pos, rows):
    return (pad_pos.x < rows) or (pad_pos.y < rows) or ((bga_info.columns - pad_pos.x) <= rows) or ((bga_info.rows - pad_pos.y) <= rows)

def is_edge_layer(bga_info, pad_pos, rows):
    return is_pad_outer_ring(bga_info, pad_pos, rows) and \
           (((pad_pos.x >= rows) and ((bga_info.columns - pad_pos.x) > rows)) !=
            ((pad_pos.y >= rows) and ((bga_info.rows - pad_pos.y) > rows)))

def get_net_classes(board, vias, except_names):
    net_list = list({via.GetNet().GetClassName() for via in vias})
    net_list = [n for n in net_list if n not in except_names]
    return [n for n in net_list if n != "Default"]

def get_signal_layers(board):
    return [layer for layer in board.GetEnabledLayers().Seq() if IsCopperLayer(layer) and board.GetLayerType(layer) == LT_SIGNAL]

def get_all_pads(board, from_footprint):
    pads = []
    for fp in board.GetFootprints():
        if fp != from_footprint:
            pads.extend(fp.Pads())
    return pads

def get_connection_dest(via, all_pads):
    net_name = via.GetNet().GetNetname()
    connected = [p for p in all_pads if p.GetNet().GetNetname() == net_name]
    if not connected:
        return wxPoint(0, 0)
    total = reduce(lambda a, b: a + b.GetPosition(), connected, wxPoint(0, 0))
    return wxPoint(total.x // len(connected), total.y // len(connected))

def pos_to_local(mod_info, via):
    pos = via.GetPosition()
    ofs = pos - mod_info.center
    px = int(round(ofs.x / float(mod_info.spacing))) + mod_info.columns // 2
    py = int(round(ofs.y / float(mod_info.spacing))) + mod_info.rows // 2
    return wxPoint(px, py)

# --- Robust netclass lookup for KiCad 10 ---
def get_netclass_for_pad(board, pad):
    """Return NETCLASS object for the given pad's net."""
    net = pad.GetNet()
    netclasses = board.GetNetClasses()
    netclass_name = net.GetClassName()
    
    # Try to get netclass via different possible methods
    netclass = None
    if hasattr(netclasses, 'GetNetClassByName'):
        netclass = netclasses.GetNetClassByName(netclass_name)
    elif hasattr(netclasses, 'Find'):
        result = netclasses.Find(netclass_name)
        # Check if result is a valid NETCLASS (has GetViaDiameter method)
        if result is not None and hasattr(result, 'GetViaDiameter'):
            netclass = result
        else:
            netclass = None
    # Fallback to default netclass
    if netclass is None:
        netclass = netclasses.GetDefault()
    return netclass

def get_via_diameter(board, pad):
    netclass = get_netclass_for_pad(board, pad)
    return netclass.GetViaDiameter()

def get_via_drill(board, pad):
    netclass = get_netclass_for_pad(board, pad)
    return netclass.GetViaDrill()

def get_clearance(board, pad):
    netclass = get_netclass_for_pad(board, pad)
    return netclass.GetClearance()
