import java.awt.*;
import java.awt.geom.Path2D;
import java.awt.image.BufferedImage;
import java.nio.file.*;
import java.util.regex.*;
import javax.imageio.ImageIO;
import javax.xml.parsers.DocumentBuilderFactory;
import org.w3c.dom.*;

/** Render the small SVG subset emitted by figures.py using only the JDK. */
class RenderFigure {
  static Color color(String s) { return Color.decode(s.equals("white")?"#ffffff":s); }
  static void draw(Element e, Graphics2D graphics) {
    Graphics2D g=(Graphics2D)graphics.create();
    if(e.hasAttribute("fill") && !e.getAttribute("fill").equals("none"))g.setColor(color(e.getAttribute("fill")));
    if(e.hasAttribute("font-size"))g.setFont(g.getFont().deriveFont(Float.parseFloat(e.getAttribute("font-size"))));
    switch(e.getTagName()) {
      case "rect":
        g.fillRect(0,0,1000,550);break;
      case "path": {
        Path2D p=new Path2D.Double();
        Matcher matcher=Pattern.compile("[MHV]|[-+]?(?:[0-9]*\\.)?[0-9]+(?:[eE][-+]?[0-9]+)?").matcher(e.getAttribute("d"));
        java.util.List<String> tokens=new java.util.ArrayList<>();while(matcher.find())tokens.add(matcher.group());
        double x=0,y=0;
        for(int i=0;i<tokens.size();) {
          String action=tokens.get(i++);
          if(action.equals("M")){x=Double.parseDouble(tokens.get(i++));y=Double.parseDouble(tokens.get(i++));p.moveTo(x,y);}
          else if(action.equals("H")){x=Double.parseDouble(tokens.get(i++));p.lineTo(x,y);}
          else if(action.equals("V")){y=Double.parseDouble(tokens.get(i++));p.lineTo(x,y);}
          else throw new IllegalArgumentException("Unsupported SVG path command");
        }
        g.setColor(color(e.getAttribute("stroke")));
        g.setStroke(new BasicStroke(e.hasAttribute("stroke-width")?Float.parseFloat(e.getAttribute("stroke-width")):1f));
        g.draw(p);break;
      }
      case "text": {
        String text=e.getTextContent();float x=Float.parseFloat(e.getAttribute("x")),y=Float.parseFloat(e.getAttribute("y"));
        int width=g.getFontMetrics().stringWidth(text);
        if(e.getAttribute("text-anchor").equals("end"))x-=width;
        if(e.getAttribute("text-anchor").equals("middle"))x-=width/2f;
        g.drawString(text,x,y);break;
      }
      default:
        NodeList children=e.getChildNodes();
        for(int i=0;i<children.getLength();i++)if(children.item(i) instanceof Element)draw((Element)children.item(i),g);
    }
    g.dispose();
  }
  public static void main(String[] args) throws Exception {
    DocumentBuilderFactory f=DocumentBuilderFactory.newInstance();
    f.setFeature("http://apache.org/xml/features/disallow-doctype-decl",true);
    Element svg=f.newDocumentBuilder().parse(Path.of(args[0]).toFile()).getDocumentElement();
    BufferedImage image=new BufferedImage(2000,1100,BufferedImage.TYPE_INT_RGB);
    Graphics2D g=image.createGraphics();g.scale(2,2);g.setFont(new Font(Font.SANS_SERIF,Font.PLAIN,14));
    g.setRenderingHint(RenderingHints.KEY_ANTIALIASING,RenderingHints.VALUE_ANTIALIAS_ON);
    draw(svg,g);g.dispose();ImageIO.write(image,"png",Path.of(args[1]).toFile());
  }
}
