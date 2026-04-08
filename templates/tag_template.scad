// Parameters supplied from CSV / external script
// These variables will be overridden by the generate_3mf_tags.py script

// CRITICAL: When executing directly from command line, ALWAYS use single quotes around each parameter ASSIGNMENT:
// CORRECT:   openscad -o output.3mf -D 'export_part="all"' -D 'color_name="TestColor"' tagGenerator/TagSeparate.scad
// INCORRECT: openscad -o output.3mf -D export_part="all" -D color_name="TestColor" tagGenerator/TagSeparate.scad
//
// Without proper quoting, OpenSCAD will interpret "all" and "TestColor" as variable names, not string values,
// generating "Ignoring unknown variable" warnings and using undefined values.

// Default values (will be overridden by command line parameters)
export_part = "all"; // Default, overridden by -D. Options: "all", "base", "frame"
color_name = "Copper Brown Metallic";
filament_type = "PLA Basic Gradient";
color_code = "10906";

// Debug parameters
echo("PARAM export_part:", export_part);
echo("PARAM color_name:", color_name);
echo("PARAM filament_type:", filament_type);
echo("PARAM color_code:", color_code);

//-------------------------------------------------
// ⇣ fixed configuration
base_stl  = "tag_base.stl";
frame_stl = "tag_frame.stl";

base_h   = 0.00;      // Base sits at Z = 0
frame_h  = 2.00;      // actual frame height (mm)

text_th         = 0.76;  // text thickness — extends through entire frame + 0.01mm
text_top_offset = -0.625; // align text top with frame top

tag_w = 62;
tag_h = 12;

margin_x     = 1.5;
margin_top   = 1.1;
line_spacing = 9.5;

/*─────────────────────────────────────────────
   ❶  RAW GEOMETRY (without color)
─────────────────────────────────────────────*/
module tag_base_raw() {
    rotate([0,0,180])
        translate([0,0, base_h/2])
            import(base_stl, center=true);
}

module tag_frame_text_raw() {
    union() {
        /* Frame — nudge 0.01mm into base to avoid floating region */
        rotate([0,0,180])
            translate([0,0, base_h + frame_h/2 - 0.01])
                import(frame_stl, center=true);

        /* Text — top aligns with frame top + offset */
        translate([0,0,
                   base_h + frame_h   // frame top
                   - text_th          // subtracting text thickness
                   + text_top_offset])
            linear_extrude(height = text_th)
            {
                /* line 1 */
                y1 = tag_h/2 - margin_top;
                translate([0, y1])
                    text(color_name,
                         size   = 3.9,
                         halign = "center",
                         valign = "top",
                         font   = "Overpass:style=Bold");

                /* line 2 */
                y2 = y1 - line_spacing;

                translate([-tag_w/2 + margin_x, y2])
                    text(filament_type,
                         size   = 3.3,
                         halign = "left",
                         valign = "baseline",
                         font   = "Overpass:style=Bold");

                translate([ tag_w/2 - margin_x, y2])
                    text(color_code,
                         size   = 3.3,
                         halign = "right",
                         valign = "baseline",
                         font   = "Overpass:style=Bold");
            }
    }
}

/*─────────────────────────────────────────────
   ❷  WRAPPER MODULES for 3MF export
   Creating separate objects with distinct colors
─────────────────────────────────────────────*/

// Wrapper module for the base with white color
module tag_base() {
    color([1,1,1,1]) // White color
    tag_base_raw();
}

// Wrapper module for the frame and text with black color
module tag_frame_text() {
    color([0,0,0,1]) // Black color
    tag_frame_text_raw();
}

/*─────────────────────────────────────────────
   ❸  EXPORTING MODEL
   Module selection based on export_part parameter
─────────────────────────────────────────────*/

if (export_part == "base") {
    // Render only the raw base component for STL export
    tag_base_raw();
} 
else if (export_part == "frame") {
    // Render only the raw frame and text component for STL export
    tag_frame_text_raw();
} 
else { // Default: "all" or undefined - render both components with color for direct SCAD use/3MF
    tag_base();        // Coloured base
    tag_frame_text();  // Coloured frame and text
}